# import os
# import time
# import torch

# from configuration import config
# from datasets import *

# from methods.er_baseline import ER
# from methods.clib import CLIB
# from methods.L2P import L2P
# from methods.our_method import Ours
# # from methods.L2P_rebuild import L2P

# torch.autograd.set_detect_anomaly(True)
# methods = { "er": ER, "clib":CLIB, 'L2P':L2P,'Ours':Ours }
# os.environ["TORCH_DISTRIBUTED_DEBUG"] = "DETAIL"

# import torch
import os
import time
import torch

from configuration import config
from datasets import *

from methods.er_baseline import ER
from methods.clib import CLIB
# from methods.L2P import L2P
from methods.L2P import L2P
from methods.rainbow_memory import RM
from methods.Finetuning import FT
from methods.lwf import LwF
from methods.our_method import Ours

torch.autograd.set_detect_anomaly(True)
torch.backends.cudnn.enabled = False
methods = { "er": ER, "clib":CLIB, 'L2P':L2P, 'rm':RM, 'Finetuning':FT, 'lwf':LwF,'Ours':Ours}
os.environ["CUDA_LAUNCH_BLOCKING"]="1"
def main():
    # Get Configurations
    args = config.base_parser()
    print(args)
    trainer = methods[args.mode](**vars(args))
    trainer.run()

if __name__ == "__main__":
    main()
    time.sleep(60)
