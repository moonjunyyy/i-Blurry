import os
import random
import time
import datetime
import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.multiprocessing as mp
import os
import random
from collections import defaultdict
import numpy as np
import torch
from randaugment import RandAugment
from torch import nn
from torch.utils.tensorboard import SummaryWriter
from torch.utils.data import DataLoader
from torchvision import transforms
from utils.onlinesampler import OnlineSampler, OnlineTestSampler
from utils.augment import Cutout
from utils.data_loader import get_statistics
from datasets import *
from utils.train_utils import select_model, select_optimizer, select_scheduler
import copy

from utils.memory import Memory

########################################################################################################################
# This is trainer with a DistributedDataParallel                                                                       #
# Based on the following tutorial:                                                                                     #
# https://github.com/pytorch/examples/blob/main/imagenet/main.py                                                       #
# And Deit by FaceBook                                                                                                 #
# https://github.com/facebookresearch/deit                                                                             #
########################################################################################################################

class _Trainer():
    def __init__(self, *args, **kwargs) -> None:
        self.mode    = kwargs.get("mode")
        self.dataset = kwargs.get("dataset")
        
        self.n_tasks = kwargs.get("n_tasks")
        self.n   = kwargs.get("n")
        self.m   = kwargs.get("m")
        self.rnd_NM  = kwargs.get("rnd_NM")
        self.rnd_seed    = kwargs.get("rnd_seed")

        self.memory_size = kwargs.get("memory_size")
        self.log_path    = kwargs.get("log_path")
        self.model_name  = kwargs.get("model_name")
        self.opt_name    = kwargs.get("opt_name")
        self.sched_name  = kwargs.get("sched_name")
        self.batchsize  = kwargs.get("batchsize")
        self.n_worker    = kwargs.get("n_worker")
        self.lr  = kwargs.get("lr")

        self.init_model  = kwargs.get("init_model")
        self.init_opt    = kwargs.get("init_opt")
        self.topk    = kwargs.get("topk")
        self.use_amp = kwargs.get("use_amp")
        self.transforms  = kwargs.get("transforms")

        self.reg_coef    = kwargs.get("reg_coef")

        self.data_dir    = kwargs.get("data_dir")
        self.debug   = kwargs.get("debug")
        self.note    = kwargs.get("note")
        
        self.eval_period     = kwargs.get("eval_period")
        self.temp_batchsize  = kwargs.get("temp_batchsize")
        self.online_iter     = kwargs.get("online_iter")
        self.num_gpus    = kwargs.get("num_gpus")
        self.workers_per_gpu     = kwargs.get("workers_per_gpu")
        self.imp_update_period   = kwargs.get("imp_update_period")

        self.dist_backend = 'nccl'
        self.dist_url = 'env://'

        self.lr_step     = kwargs.get("lr_step")    # for adaptive LR
        self.lr_length   = kwargs.get("lr_length")  # for adaptive LR
        self.lr_period   = kwargs.get("lr_period")  # for adaptive LR

        self.memory_epoch    = kwargs.get("memory_epoch")    # for RM
        self.distilling  = kwargs.get("distilling") # for BiC
        self.agem_batch  = kwargs.get("agem_batch") # for A-GEM
        self.mir_cands   = kwargs.get("mir_cands")  # for MIR

        self.start_time = time.time()
        self.num_updates = 0
        self.train_count = 0

        self.ngpus_per_nodes = torch.cuda.device_count()
        
        if "WORLD_SIZE" in os.environ:
            self.world_size  = int(os.environ["WORLD_SIZE"]) * self.ngpus_per_nodes
        else:
            self.world_size  = self.world_size * self.ngpus_per_nodes
        self.distributed     = self.world_size > 1

        if self.distributed:
            self.batchsize = self.batchsize // self.world_size
        if self.temp_batchsize is None:
            self.temp_batchsize = self.batchsize // 2
        if self.temp_batchsize > self.batchsize:
            self.temp_batchsize = self.batchsize
        self.memory_batchsize = self.batchsize - self.temp_batchsize

        self.exposed_classes = []
        # # Set the logger
        # logging.config.fileConfig("./configuration/logging.conf")
        # self.logger  = logging.getLogger()

        
        os.makedirs(f"{self.log_path}/logs/{self.dataset}/{self.note}", exist_ok=True)
        os.makedirs(f"{self.log_path}/tensorboard/{self.dataset}/{self.note}", exist_ok=True)
        # fileHandler  = logging.FileHandler(f'{self.log_path}/logs/{self.dataset}/{self.note}/seed_{self.rnd_seed}.log', mode="w")

        # formatter    = logging.Formatter(
        #     "[%(levelname)s] %(filename)s:%(lineno)d > %(message)s"
        # )
        # fileHandler.setFormatter(formatter)
        # self.logger.addHandler(fileHandler)

        return

    def setup_dataset_for_distributed(self):

        datasets = {
        "cifar10": CIFAR10,
        "cifar100": CIFAR100,
        "svhn": SVHN,
        "fashionmnist": FashionMNIST,
        "mnist": MNIST,
        "tinyimagenet": TinyImageNet,
        "notmnist": NotMNIST,
        "cub200": CUB200,
        "imagenet": ImageNet
        }

        mean, std, n_classes, inp_size, _ = get_statistics(dataset=self.dataset)
        self.n_classes = n_classes

        train_transform = []
        if self.model_name == 'vit':
            inp_size = 224
        self.cutmix = "cutmix" in self.transforms 
        if "cutout" in self.transforms:
            train_transform.append(Cutout(size=16))
            if self.gpu_transform:
                self.gpu_transform = False
                # self.logger.warning("cutout not supported on GPU!")
        if "randaug" in self.transforms:
            train_transform.append(RandAugment())
            if self.gpu_transform:
                self.gpu_transform = False
                # self.logger.warning("randaug not supported on GPU!")
        if "autoaug" in self.transforms:
            if 'cifar' in self.dataset:
                train_transform.append(transforms.AutoAugment(transforms.AutoAugmentPolicy('cifar10')))
            elif 'imagenet' in self.dataset:
                train_transform.append(transforms.AutoAugment(transforms.AutoAugmentPolicy('imagenet')))
            elif 'svhn' in self.dataset:
                train_transform.append(transforms.AutoAugment(transforms.AutoAugmentPolicy('svhn')))
                
        self.train_transform = transforms.Compose([
                transforms.Resize((inp_size, inp_size)),
                transforms.RandomCrop(inp_size, padding=4),
                transforms.RandomHorizontalFlip(),
                *train_transform,
                transforms.ToTensor(),
                transforms.Normalize(mean, std),])
        print(f"Using train-transforms {train_transform}")
        self.test_transform = transforms.Compose([
                transforms.Resize((inp_size, inp_size)),
                transforms.ToTensor(),
                transforms.Normalize(mean, std),])

        _r = dist.get_rank() if self.distributed else None       # means that it is not distributed
        _w = dist.get_world_size() if self.distributed else None # means that it is not distributed

        self.train_dataset   = datasets[self.dataset](root=self.data_dir, train=True,  download=True, 
                                                      transform=transforms.ToTensor())
        self.test_dataset    = datasets[self.dataset](root=self.data_dir, train=False, download=True, transform=self.test_transform)
        self.train_sampler   = OnlineSampler(self.train_dataset, self.n_tasks, self.m, self.n, self.rnd_seed, 0, self.rnd_NM, _w, _r)
        self.test_sampler    = OnlineTestSampler(self.test_dataset, [], _w, _r)

        self.train_dataloader    = DataLoader(self.train_dataset, batch_size=self.temp_batchsize, sampler=self.train_sampler, num_workers=self.n_worker)
        
        self.seen = 0
        self.memory = Memory(self.train_dataset)

    def setup_distributed_model(self):

        print("Building model...")
        self.model = select_model(self.model_name, self.dataset, 1)
        self.scaler = torch.cuda.amp.GradScaler(enabled=self.use_amp)
        self.writer = SummaryWriter(f"{self.log_path}/tensorboard/{self.dataset}/{self.note}/seed_{self.rnd_seed}")
        
        self.model.to(self.device)
        self.model_without_ddp = self.model
        if self.distributed:
            self.model = torch.nn.parallel.DistributedDataParallel(self.model)
            self.model._set_static_graph()
            self.model_without_ddp = self.model.module
        self.criterion = self.model_without_ddp.loss_fn if hasattr(self.model_without_ddp, "loss_fn") else nn.CrossEntropyLoss(reduction="mean")
        self.optimizer = select_optimizer(self.opt_name, self.lr, self.model)
        self.scheduler = select_scheduler(self.sched_name, self.optimizer)

        n_params = sum(p.numel() for p in self.model_without_ddp.parameters())
        print(f"Total Parameters :\t{n_params}")
        n_params = sum(p.numel() for p in self.model_without_ddp.parameters() if p.requires_grad)
        print(f"Learnable Parameters :\t{n_params}")
        print("")

    def run(self):
        # Distributed Launch
        # mp.set_start_method('spawn')
        if self.ngpus_per_nodes > 1:
            # processes = []
            # for i in range(0, self.ngpus_per_nodes):
            #     p = mp.Process(target=self.main_worker, args=(i,))
            #     processes.append(p)
            #     p.start()
            # for p in processes:
            #     p.join()
            mp.spawn(self.main_worker, nprocs=self.ngpus_per_nodes, join=True)
        else:
            self.main_worker(0)
    
    def main_worker(self, gpu) -> None:
        self.gpu    = gpu % self.ngpus_per_nodes
        self.device = torch.device(self.gpu)
        if self.distributed:
            self.local_rank = self.gpu
            if 'SLURM_PROCID' in os.environ.keys():
                self.rank = int(os.environ['SLURM_PROCID']) * self.ngpus_per_nodes + self.gpu
                print(f"| Init Process group {os.environ['SLURM_PROCID']} : {self.local_rank}")
            else :
                self.rank = self.gpu
                print(f"| Init Process group 0 : {self.local_rank}")
            if 'MASTER_ADDR' not in os.environ.keys():
                os.environ['MASTER_ADDR'] = '127.0.0.1'
                os.environ['MASTER_PORT'] = '12701'
            torch.cuda.set_device(self.gpu)
            time.sleep(self.rank * 0.1) # prevent port collision
            dist.init_process_group(backend=self.dist_backend, init_method=self.dist_url,
                                    world_size=self.world_size, rank=self.rank)
            self.setup_for_distributed(self.is_main_process())
        else:
            pass

        self.setup_dataset_for_distributed()
        self.total_samples = len(self.train_dataset)

        print(f"[1] Select a CIL method ({self.mode})")
        self.setup_distributed_model()

        if self.rnd_seed is not None:
            rnd_seed = self.rnd_seed
            random.seed(rnd_seed)
            torch.manual_seed(rnd_seed)
            cudnn.deterministic = True
            np.random.seed(self.rnd_seed)
            print('You have chosen to seed training. '
                'This will turn on the CUDNN deterministic setting, '
                'which can slow down your training considerably! '
                'You may see unexpected behavior when restarting '
                'from checkpoints.')
        cudnn.benchmark = True
    
        print(f"[2] Incrementally training {self.n_tasks} tasks")
        task_records = defaultdict(list)
        eval_results = defaultdict(list)
        samples_cnt = 0

        num_eval = self.eval_period
        for task_id in range(self.n_tasks):
            if self.mode == "joint" and task_id > 0:
                return
            print("\n" + "#" * 50)
            print(f"# Task {task_id} iteration")
            print("#" * 50 + "\n")
            print("[2-1] Prepare a datalist for the current task")
            self.train_sampler.set_task(task_id)
            
            self.online_before_task(task_id)
            for i, (image, label) in enumerate(self.train_dataloader):
                if self.debug and i >= 100 : break
                samples_cnt += image.size(0)
                if samples_cnt > num_eval:
                # if samples_cnt % args.eval_period == 0:
                    test_sampler = OnlineTestSampler(self.test_dataset, self.exposed_classes)
                    test_dataloader = DataLoader(self.test_dataset, batch_size=self.batchsize*2, sampler=test_sampler, num_workers=self.n_worker)
                    eval_dict = self.online_evaluate(test_dataloader)
                    if self.distributed:
                        eval_dict =  torch.tensor([eval_dict['avg_loss'], eval_dict['avg_acc'], *eval_dict['cls_acc']], device=self.device)
                        dist.all_reduce(eval_dict, op=dist.ReduceOp.SUM)
                        eval_dict = eval_dict.cpu().numpy()
                        eval_dict = {'avg_loss': eval_dict[0]/self.world_size, 'avg_acc': eval_dict[1]/self.world_size, 'cls_acc': eval_dict[2:]/self.world_size}
                    eval_results["test_acc"].append(eval_dict['avg_acc'])
                    eval_results["avg_acc"].append(eval_dict['cls_acc'])
                    eval_results["data_cnt"].append(num_eval)
                    self.report_test(num_eval, eval_dict["avg_loss"], eval_dict['avg_acc'])
                    num_eval += self.eval_period
                loss, acc = self.online_step([image,label], samples_cnt)
                self.report_training(samples_cnt, loss, acc)
            self.online_after_task(task_id)
            test_sampler = OnlineTestSampler(self.test_dataset, self.exposed_classes)
            test_dataloader = DataLoader(self.test_dataset, batch_size=self.batchsize*2, sampler=test_sampler, num_workers=self.n_worker)
            eval_dict = self.online_evaluate(test_dataloader)
            if self.distributed:
                eval_dict =  torch.tensor([eval_dict['avg_loss'], eval_dict['avg_acc'], *eval_dict['cls_acc']], device=self.device)
                dist.all_reduce(eval_dict, op=dist.ReduceOp.SUM)
                eval_dict = eval_dict.cpu().numpy()
                eval_dict = {'avg_loss': eval_dict[0]/self.world_size, 'avg_acc': eval_dict[1]/self.world_size, 'cls_acc': eval_dict[2:]/self.world_size}
            task_acc = eval_dict['avg_acc']

            print("[2-4] Update the information for the current task")
            task_records["task_acc"].append(task_acc)
            task_records["cls_acc"].append(eval_dict["cls_acc"])

            print("[2-5] Report task result")
            self.writer.add_scalar("Metrics/TaskAcc", task_acc, task_id)
        
        np.save(f"{self.log_path}/logs/{self.dataset}/{self.note}/seed_{self.rnd_seed}.npy", task_records["task_acc"])

        if self.mode == 'gdumb':
            eval_results, task_records = self.evaluate_all(self.test_dataset, self.memory_epoch, self.batchsize, self.n_worker)
        if self.eval_period is not None:
            np.save(f'{self.log_path}/logs/{self.dataset}/{self.note}/seed_{self.rnd_seed}_eval.npy', eval_results['test_acc'])
            np.save(f'{self.log_path}/logs/{self.dataset}/{self.note}/seed_{self.rnd_seed}_eval_time.npy', eval_results['data_cnt'])

        # Accuracy (A)
        A_auc = np.mean(eval_results["test_acc"])
        A_avg = np.mean(task_records["task_acc"])
        A_last = task_records["task_acc"][self.n_tasks - 1]

        # Forgetting (F)
        cls_acc = np.array(task_records["cls_acc"])
        acc_diff = []
        for j in range(self.n_classes):
            if np.max(cls_acc[:-1, j]) > 0:
                acc_diff.append(np.max(cls_acc[:-1, j]) - cls_acc[-1, j])
        F_last = np.mean(acc_diff)

        print(f"======== Summary =======")
        print(f"A_auc {A_auc} | A_avg {A_avg} | A_last {A_last} | F_last {F_last}")
    
    def add_new_class(self, class_name):
        self.exposed_classes.append(class_name)
        self.num_learned_class = len(self.exposed_classes)
        prev_weight = copy.deepcopy(self.model_without_ddp.fc.weight.data)
        self.model_without_ddp.fc = nn.Linear(self.model_without_ddp.fc.in_features, self.num_learned_class).to(self.device)
        with torch.no_grad():
            if self.num_learned_class > 1:
                self.model_without_ddp.fc.weight[:self.num_learned_class - 1] = prev_weight
        for param in self.optimizer.param_groups[1]['params']:
            if param in self.optimizer.state.keys():
                del self.optimizer.state[param]
        del self.optimizer.param_groups[1]
        params = [param for name, param in self.model.named_parameters() if 'fc' in name]
        self.optimizer.add_param_group({'params': params})
        self.memory.add_new_class(cls_list=self.exposed_classes)
        if 'reset' in self.sched_name:
            self.update_schedule(reset=True)

    def online_step(self, sample, samples_cnt):
        raise NotImplementedError()

    def online_before_task(self, task_id):
        raise NotImplementedError()

    def online_after_task(self, task_id):
        raise NotImplementedError()
    
    def online_evaluate(self, test_loader, samples_cnt):
        raise NotImplementedError()
            
    def is_dist_avail_and_initialized(self):
        if not dist.is_available():
            return False
        if not dist.is_initialized():
            return False
        return True

    def get_world_size(self):
        if not self.is_dist_avail_and_initialized():
            return 1
        return dist.get_world_size()

    def get_rank(self):
        if not self.is_dist_avail_and_initialized():
            return 0
        return dist.get_rank()

    def is_main_process(self):
        return self.get_rank() == 0

    def setup_for_distributed(self, is_master):
        """
        This function disables printing when not in master process
        """
        import builtins as __builtin__
        builtin_print = __builtin__.print

        def print(*args, **kwargs):
            force = kwargs.pop('force', False)
            if is_master or force:
                builtin_print(*args, **kwargs)
        __builtin__.print = print

    def report_training(self, sample_num, train_loss, train_acc):
        self.writer.add_scalar(f"train/loss", train_loss, sample_num)
        self.writer.add_scalar(f"train/acc", train_acc, sample_num)
        print(
            f"Train | Sample # {sample_num} | train_loss {train_loss:.4f} | train_acc {train_acc:.4f} | "
            f"lr {self.optimizer.param_groups[0]['lr']:.6f} | "
            f"running_time {datetime.timedelta(seconds=int(time.time() - self.start_time))} | "
            f"ETA {datetime.timedelta(seconds=int((time.time() - self.start_time) * (self.total_samples-sample_num) / sample_num))}"
        )

    def report_test(self, sample_num, avg_loss, avg_acc):
        self.writer.add_scalar(f"test/loss", avg_loss, sample_num)
        self.writer.add_scalar(f"test/acc", avg_acc, sample_num)
        print(
            f"Test | Sample # {sample_num} | test_loss {avg_loss:.4f} | test_acc {avg_acc:.4f} | "
        )

    def _interpret_pred(self, y, pred):
        # xlable is batch
        ret_num_data = torch.zeros(self.n_classes)
        ret_corrects = torch.zeros(self.n_classes)

        xlabel_cls, xlabel_cnt = y.unique(return_counts=True)
        for cls_idx, cnt in zip(xlabel_cls, xlabel_cnt):
            ret_num_data[cls_idx] = cnt

        correct_xlabel = y.masked_select(y == pred)
        correct_cls, correct_cnt = correct_xlabel.unique(return_counts=True)
        for cls_idx, cnt in zip(correct_cls, correct_cnt):
            ret_corrects[cls_idx] = cnt

        return ret_num_data, ret_corrects