from typing import TypeVar, Iterable
import timm
import torch
import torch.nn as nn
import torch.nn.functional as F
import logging
from torch.utils.tensorboard import SummaryWriter
import timm
from timm.models.registry import register_model
from timm.models.vision_transformer import _cfg, default_cfgs

from models.vit import _create_vision_transformer

logger = logging.getLogger()
writer = SummaryWriter("tensorboard")

T = TypeVar('T', bound = 'nn.Module')

default_cfgs['vit_base_patch16_224_l2p'] = _cfg(
        url='https://storage.googleapis.com/vit_models/imagenet21k/ViT-B_16.npz',
        num_classes=21843)

# Register the backbone model to timm
@register_model
def vit_base_patch16_224_l2p(pretrained=False, **kwargs):
    """ ViT-Base model (ViT-B/32) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-21k weights @ 224x224, source https://github.com/google-research/vision_transformer.
    NOTE: this model has valid 21k classifier head and no representation (pre-logits) layer
    """
    model_kwargs = dict(
        patch_size=16, embed_dim=768, depth=12, num_heads=12, **kwargs)
    model = _create_vision_transformer('vit_base_patch16_224_l2p', pretrained=pretrained, **model_kwargs)
    return model

class Prompt(nn.Module):
    def __init__(self,
                 pool_size            : int,
                 selection_size       : int,
                 prompt_len           : int,
                 dimention            : int,
                 _diversed_selection  : bool = False,
                 _batchwise_selection : bool = False,
                 **kwargs):
        super().__init__()

        self.pool_size      = pool_size
        self.selection_size = selection_size
        self.prompt_len     = prompt_len
        self.dimention      = dimention
        self._diversed_selection  = _diversed_selection
        self._batchwise_selection = _batchwise_selection

        self.key     = nn.Parameter(torch.randn(pool_size, dimention, requires_grad= True))
        self.prompts = nn.Parameter(torch.randn(pool_size, prompt_len, dimention, requires_grad= True))
        
        torch.nn.init.uniform_(self.key,     -1, 1)
        torch.nn.init.uniform_(self.prompts, -1, 1)

        self.register_buffer('frequency', torch.ones (pool_size))
        self.register_buffer('counter',   torch.zeros(pool_size))
    
    def forward(self, query : torch.Tensor, **kwargs):

        B, D = query.shape
        assert D == self.dimention, f'Query dimention {D} does not match prompt dimention {self.dimention}'
        # Select prompts
        match = 1 - F.cosine_similarity(query.unsqueeze(1), self.key, dim=-1)
        # if self.training and self._diversed_selection:
        #     topk = match * F.normalize(self.frequency, p=1, dim=-1)
        # else:
            # topk = match
        _ ,topk = match.topk(self.selection_size, dim=-1, largest=False, sorted=True)
        # Batch-wise prompt selection
        if self._batchwise_selection:
            idx, counts = topk.unique(sorted=True, return_counts=True)
            _,  mosts  = counts.topk(self.selection_size, largest=True, sorted=True)
            topk = idx[mosts].clone().expand(B, -1)
        # Frequency counter
        self.counter += torch.bincount(topk.reshape(-1).clone(), minlength = self.pool_size)
        # selected prompts
        selection = self.prompts.repeat(B, 1, 1, 1).gather(1, topk.unsqueeze(-1).unsqueeze(-1).expand(-1, -1, self.prompt_len, self.dimention).clone())
        simmilarity = match.gather(1, topk)
        # get unsimilar prompts also 
        return simmilarity, selection

    def update(self):
        if self.training:
            self.frequency += self.counter
        counter = self.counter.clone()
        self.counter *= 0
        if self.training:
            return self.frequency - 1
        else:
            return counter

class Ours(nn.Module):
    def __init__(self,
                 pos_g_prompt   : Iterable[int] = (0,1),
                 len_g_prompt   : int   = 5,
                 pos_e_prompt   : Iterable[int] = (2,3,4),
                 len_e_prompt   : int   = 20,
                 selection_size : int   = 1,
                 prompt_func    : str   = 'prompt_tuning',
                 task_num       : int   = 10,
                 class_num      : int   = 100,
                 lambd          : float = 1.0,
                 use_mask       : bool  = True,
                 use_dyna_exp   : bool  = False,
                 use_contrastiv : bool  = False,
                 use_last_layer : bool  = True,
                 backbone_name  : str   = None,
                 selection_size = None,
                 **kwargs):
        super().__init__()

        if backbone_name is None:
            raise ValueError('backbone_name must be specified')
        self.lambd       = lambd
        self.class_num   = class_num
        self.task_num    = task_num
        self.use_mask    = use_mask
        self.use_dyna_exp    = use_dyna_exp
        self.use_contrastiv  = use_contrastiv
        self.use_last_layer  = use_last_layer
        self.selection_size  = selection_size

        self.add_module('backbone', timm.models.create_model(backbone_name, pretrained=True, num_classes=class_num,
                                                             drop_rate=0.,drop_path_rate=0.,drop_block_rate=None))
        for name, param in self.backbone.named_parameters():
                param.requires_grad = False
        self.backbone.fc.weight.requires_grad = True
        self.backbone.fc.bias.requires_grad   = True

        self.register_buffer('pos_g_prompt', torch.tensor(pos_g_prompt, dtype=torch.int64))
        self.register_buffer('pos_e_prompt', torch.tensor(pos_e_prompt, dtype=torch.int64))
        self.register_buffer('similarity', torch.zeros(1))
        # self.register_buffer('mask', torch.zeros(class_num))
        
        self.len_g_prompt = len_g_prompt
        self.len_e_prompt = len_e_prompt
        g_pool = 1
        e_pool = task_num
        self.g_length = len(pos_g_prompt) if pos_g_prompt else 0
        self.e_length = len(pos_e_prompt) if pos_e_prompt else 0
        g_pool = 1
        e_pool = task_num

        self.register_buffer('count', torch.zeros(e_pool))
        # self.register_buffer('key', torch.randn(e_pool, self.backbone.embed_dim))
        self.key     = nn.Parameter(torch.randn(e_pool, self.backbone.embed_dim))
        self.mask    = nn.Parameter(torch.zeros(e_pool, self.class_num) - 1)

        if prompt_func == 'prompt_tuning':
            self.prompt_func = self.prompt_tuning
            self.g_prompt = None if len(pos_g_prompt) == 0 else Prompt(g_pool, 1, self.g_length * self.len_g_prompt, self.backbone.num_features, batchwise_selection = False)
            self.e_prompt = None if len(pos_e_prompt) == 0 else Prompt(e_pool, self.selection_size, self.e_length * self.len_e_prompt, self.backbone.num_features, batchwise_selection = False)

        elif prompt_func == 'prefix_tuning':
            self.prompt_func = self.prefix_tuning
            self.g_size = 2 * self.g_length * self.len_g_prompt
            self.e_size = 2 * self.e_length * self.len_e_prompt
            self.g_prompts = nn.Parameter(torch.randn(g_pool, self.g_size, self.backbone.embed_dim))
            self.e_prompts = nn.Parameter(torch.randn(e_pool, self.e_size, self.backbone.embed_dim))

        self.exposed_classes = 0
    
    @torch.no_grad()
    def set_exposed_classes(self, classes):
        len_classes = self.exposed_classes
        self.exposed_classes = len(classes)
        # self.mask.data[:,len_classes:self.exposed_classes] = 0
        # self.mask.data[:, self.exposed_classes:] = -torch.inf
    
    def prompt_tuning(self,
                      x        : torch.Tensor,
                      g_prompt : torch.Tensor,
                      e_prompt : torch.Tensor,
                      **kwargs):

        B, N, C = x.size()
        g_prompt = g_prompt.contiguous().view(B, -1, self.len_g_prompt, C)
        e_prompt = e_prompt.contiguous().view(B, -1, self.len_e_prompt, C)

        for n, block in enumerate(self.backbone.blocks):
            pos_g = ((self.pos_g_prompt.eq(n)).nonzero()).squeeze()
            if pos_g.numel() != 0:
                x = torch.cat((x, g_prompt[:, pos_g]), dim = 1)

            pos_e = ((self.pos_e_prompt.eq(n)).nonzero()).squeeze()
            if pos_e.numel() != 0:
                x = torch.cat((x, e_prompt[:, pos_e]), dim = 1)
            x = block(x)
            x = x[:, :N, :]
        return x
    
    def prefix_tuning(self,
                      x        : torch.Tensor,
                      g_prompt : torch.Tensor,
                      e_prompt : torch.Tensor,
                      **kwargs):

        B, N, C = x.size()
        g_prompt = g_prompt.contiguous().view(B, -1, self.len_g_prompt, C)
        e_prompt = e_prompt.contiguous().view(B, -1, self.len_e_prompt, C)

        for n, block in enumerate(self.backbone.blocks):
            xq = block.norm1(x)
            xk = xq.clone()
            xv = xq.clone()

            pos_g = ((self.pos_g_prompt.eq(n)).nonzero()).squeeze()
            if pos_g.numel() != 0:
                xk = torch.cat((xk, g_prompt[:, pos_g * 2 + 0].clone()), dim = 1)
                xv = torch.cat((xv, g_prompt[:, pos_g * 2 + 1].clone()), dim = 1)

            pos_e = ((self.pos_e_prompt.eq(n)).nonzero()).squeeze()
            if pos_e.numel() != 0:
                xk = torch.cat((xk, e_prompt[:, pos_e * 2 + 0].clone()), dim = 1)
                xv = torch.cat((xv, e_prompt[:, pos_e * 2 + 1].clone()), dim = 1)
            
            attn   = block.attn
            weight = attn.qkv.weight
            bias   = attn.qkv.bias
            
            B, N, C = xq.shape
            xq = F.linear(xq, weight[:C   ,:], bias[:C   ]).reshape(B,  N, attn.num_heads, C // attn.num_heads).permute(0, 2, 1, 3)
            _B, _N, _C = xk.shape
            xk = F.linear(xk, weight[C:2*C,:], bias[C:2*C]).reshape(B, _N, attn.num_heads, C // attn.num_heads).permute(0, 2, 1, 3)
            _B, _N, _C = xv.shape
            xv = F.linear(xv, weight[2*C: ,:], bias[2*C: ]).reshape(B, _N, attn.num_heads, C // attn.num_heads).permute(0, 2, 1, 3)

            attention = (xq @ xk.transpose(-2, -1)) * attn.scale
            attention = attention.softmax(dim=-1)
            attention = attn.attn_drop(attention)

            attention = (attention @ xv).transpose(1, 2).reshape(B, N, C)
            attention = attn.proj(attention)
            attention = attn.proj_drop(attention)

            x = x + block.drop_path1(block.ls1(attention))
            x = x + block.drop_path2(block.ls2(block.mlp(block.norm2(x))))
        return x

    def forward(self, inputs, only_feat = False) :
        self.backbone.eval()
        with torch.no_grad():
            x = self.backbone.patch_embed(inputs)
            B, N, D = x.size()

            cls_token = self.backbone.cls_token.expand(B, -1, -1)
            token_appended = torch.cat((cls_token, x), dim=1)
            x = self.backbone.pos_drop(token_appended + self.backbone.pos_embed)
            query = x.clone()
            for n, block in enumerate(self.backbone.blocks):
                if n == len(self.backbone.blocks) - 1 and not self.use_last_layer: break
                query = block(query)
            query = query[:, 0]

        distance = 1 - F.cosine_similarity(query.unsqueeze(1), self.key, dim=-1)
        if self.use_contrastiv:
            mass = self.count + 1
        if self.use_dyna_exp:
            if self.training:
                key_range   = 500 / (self.count + 1)
                in_range    = distance < key_range
                no_match    = (in_range.sum(-1) == 0).nonzero().squeeze()
                if no_match.numel() != 0:
                    if no_match.numel() == 1:
                        no_match = no_match.unsqueeze(0)
                    with torch.no_grad():
                        self.count = torch.cat((self.count, torch.zeros(no_match.shape[0], device=self.count.device)), dim=0)
                        self.key   = nn.Parameter(torch.cat((self.key, query[no_match].clone()), dim=0))
                        self.mask  = nn.Parameter(torch.cat((self.mask, torch.zeros(no_match.shape[0], self.class_num, device=self.mask.device)), dim=0))
                        self.e_prompts = nn.Parameter(torch.cat((self.e_prompts, self.e_prompts[distance[no_match].argmin(dim=-1)].clone()), dim=0))
                    distance = 1 - F.cosine_similarity(query.unsqueeze(1), self.key, dim=-1)
                    key_range   = 500 / (self.count + 1)
                    out_range   = distance > key_range
                    distance[out_range] = 2
                    if self.use_contrastiv:
                        mass = self.count + 1
        distance = distance * mass
        topk = distance.topk(self.selection_size, dim=1, largest=False)[1]
        distance = distance[torch.arange(topk.size(0), device=topk.device).unsqueeze(1).repeat(1,self.selection_size), topk].squeeze().clone()
        e_prompts = self.e_prompts[topk].squeeze().clone()
        mask = self.mask[topk].mean(1).squeeze().clone()

        g_prompts = self.g_prompts[0].repeat(B, 1, 1)
        if self.training:
            with torch.no_grad():
                num = topk.view(-1).bincount(minlength=self.e_prompts.size(0))
                self.count += num

        x = self.prompt_func(self.backbone.pos_drop(token_appended + self.backbone.pos_embed), g_prompts, e_prompts)
        x = self.backbone.norm(x)
        # x = x.mean(dim=1).squeeze()
        x = self.backbone.fc_norm(x[:, 0])
        x = self.backbone.fc(x)

        if self.use_mask:
            # self.mask_l1_loss = mask.abs().mean()
            mask = torch.sigmoid(mask)
            mask_prob = mask / mask.sum(dim=1, keepdim=True)
            # self.mask_entropy_loss = (- mask_prob * (mask_prob + 1e-5).log()).mean()
            self.mask_entropy_loss = (- mask * (mask - 1)).mean()
            mask = mask * 2.0
            # self.mask_limit_loss = (mask.sum() - 10).abs()
        else:
            self.mask_entropy_loss = 0

        if self.use_contrastiv:
            key_wise_distance = 1 - F.cosine_similarity(self.key.unsqueeze(1), self.key, dim=-1)
            # self.similarity_loss = -((key_wise_distance / mass).exp().sum() / ((distance / mass[topk]).exp().sum() + (key_wise_distance / mass).exp().sum()) + 1e-6).log()
            # self.similarity_loss = -(key_wise_distance / mass).mean() + (distance / mass[topk]).mean()
            self.similarity_loss = -((key_wise_distance[topk] / mass[topk]).exp().sum() / ((distance / mass[topk]).exp().sum() + (key_wise_distance[topk] / mass[topk]).exp().sum()) + 1e-6).log()
        else:
            self.similarity_loss = distance.mean()

        if self.use_mask:
            x = x * mask
        return x
    
    def loss_fn(self, output, target):
        # B, C = output.size()
        # smooth_target = torch.zeros_like(output)
        # smooth_target[:self.exposed_classes] = .1 / self.exposed_classes
        # smooth_target[torch.arange(target.size(0), device=target.device),target] += .9
        # return F.cross_entropy(output[:,:self.exposed_classes], smooth_target[:,:self.exposed_classes]) + self.similarity_loss #+ self.mask_entropy_loss # + self.mask_limit_loss
        return F.cross_entropy(output, target) + self.similarity_loss #+ self.mask_entropy_loss # + self.mask_limit_loss

    def convert_train_task(self, task : torch.Tensor, **kwargs):
    
        # task = torch.tensor(task,dtype=torch.float)
        # flag = -1
        # for n, t in enumerate(self.tasks):
        #     if torch.equal(t, task):
        #         flag = n
        #         break
        # if flag == -1:
        #     self.tasks.append(task)
        #     self.task_id = len(self.tasks) - 1
        #     if self.training:
        #         if self.task_id != 0:
        #             with torch.no_grad():
        #                 # self.e_prompt.prompts[self.task_id] = self.e_prompt.prompts[self.task_id - 1].detach().clone()
        #                 # self.e_prompt.key[self.task_id] = self.e_prompt.key[self.task_id - 1].detach().clone()
        #                 self.e_prompt.prompts[self.task_id] = self.e_prompt.prompts[self.task_id - 1].clone()
        #                 self.e_prompt.key[self.task_id] = self.e_prompt.key[self.task_id - 1].clone()
        # else :
        #     self.task_id = flag

        self.mask += -torch.inf
        self.mask[task] = 0
        
    def get_count(self):
        return self.prompt.update()