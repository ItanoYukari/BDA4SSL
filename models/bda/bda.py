from models.nets.net import *
import models.nets.prern as pnet
from torch import optim
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
from torch.cuda.amp import autocast, GradScaler
from random import randint
from types import MethodType,FunctionType
import numpy as np
import os
import contextlib
from train_utils import AverageMeter

from .bda_utils import consistency_loss_bda
from train_utils import ce_loss


class TotalNet(nn.Module):
    def __init__(self, net_builder, num_classes, net_name):
        super(TotalNet, self).__init__()
        if net_name=='resnet18':
            base_net = net_builder(num_classes=num_classes)    
            self.feature_extractor = ResNet18(num_classes, base_net)  
        elif net_name=='cnn13':
            self.feature_extractor = cnn13(num_classes=num_classes)  
        elif net_name=='preresnet':
            self.feature_extractor = pnet.ResNet18(num_classes=num_classes)  
        else:
            self.feature_extractor = net_builder(num_classes=num_classes)                  
        classifier_output_dim = num_classes
        self.classifier_reverse = ReverseCLS(self.feature_extractor.output_num(), classifier_output_dim)
        
    def forward(self, x):
        f = self.feature_extractor(x)
        f, _, __, y = self.classifier_reverse(f)
        return y

class BDA:
    def __init__(self, net_builder, num_classes, ema_m, T, lambda_u,\
                 it=0, num_eval_iter=1000, tb_log=None, logger=None, net_name=''):
        
        super(BDA, self).__init__()
        self.flag = True

        self.loader = {}
        self.num_classes = num_classes
        self.ema_m = ema_m

        # create the encoders
        # network is builded only by num_classes,
        # other configs are covered in main.py
        
        self.train_model = TotalNet(net_builder, num_classes, net_name) 
        self.eval_model = TotalNet(net_builder, num_classes, net_name) 
        self.num_eval_iter = num_eval_iter
        self.lambda_u = lambda_u
        self.tb_log = tb_log
        
        self.optimizer = None
        self.scheduler = None
        
        self.it = 0
        self.label_fix = None
        self.not_class = None
        self.highest_pseduo_acc = 0.0
        
        self.logger = logger
        self.print_fn = print if logger is None else logger.info
        
        # initialize
        for param_q, param_k in zip(self.train_model.parameters(), self.eval_model.parameters()):
            param_k.data.copy_(param_q.detach().data)  
            param_k.requires_grad = False  
            
        self.eval_model.eval()
            
            
    @torch.no_grad()
    def _eval_model_update(self):
        """
        Momentum update of evaluation model (exponential moving average)
        """
        for param_train, param_eval in zip(self.train_model.parameters(), self.eval_model.parameters()):
            param_eval.copy_(param_eval * self.ema_m + param_train.detach() * (1-self.ema_m))
        
        for buffer_train, buffer_eval in zip(self.train_model.buffers(), self.eval_model.buffers()):
            buffer_eval.copy_(buffer_train)            
    
     
    def set_data_loader(self, loader_dict):
        self.loader_dict = loader_dict
        self.print_fn(f'[!] data loader keys: {self.loader_dict.keys()}')    
            
    
    def set_optimizer(self, optimizer, scheduler=None):
        self.optimizer = optimizer
        self.scheduler = scheduler
    
    
    def train(self, args, logger=None):
        """
        Train function of BDA.
        From data_loader, it inference training data, computes losses, and update the networks.
        """

        feature_extractor = self.train_model.module.feature_extractor.train(True) if hasattr(self.train_model, 'module') else self.train_model.feature_extractor.train(True)
        cls_reverse = self.train_model.module.classifier_reverse.train(True) if hasattr(self.train_model, 'module') else self.train_model.classifier_reverse.train(True)
        feature_extractor.cuda(args.gpu)
        cls_reverse.cuda(args.gpu)
        ngpus_per_node = torch.cuda.device_count()

        # lb: labeled, ulb: unlabeled
        self.train_model.train()
        
        # for gpu profiling
        start_batch = torch.cuda.Event(enable_timing=True)
        end_batch = torch.cuda.Event(enable_timing=True)
        start_run = torch.cuda.Event(enable_timing=True)
        end_run = torch.cuda.Event(enable_timing=True)
        
        start_batch.record()
        best_eval_acc, best_it = 0.0, 0
        
        scaler = GradScaler()
        amp_cm = autocast if args.amp else contextlib.nullcontext

        distri = torch.ones((128,args.num_classes),dtype=torch.float32).cuda(args.gpu)
        distri_reverse = torch.ones((128,args.num_classes),dtype=torch.float32).cuda(args.gpu)
        count = 0
        
        for (x_lb, y_lb), data in zip(self.loader_dict['train_lb'], self.loader_dict['train_ulb']):                
            y_lb = y_lb.long()
            if args.dataset == 'miniimage':
                x_ulb_w = data[0][0]
                x_ulb_s = data[0][1]
            else:
                x_ulb_w = data[0]
                x_ulb_s = data[1]

            # prevent the training iterations exceed args.num_train_iter
            if self.it > args.num_train_iter:
                break
            
            end_batch.record()
            torch.cuda.synchronize()
            start_run.record()
            
            num_lb = x_lb.shape[0]
            num_ulb = x_ulb_w.shape[0]
            assert num_ulb == x_ulb_s.shape[0]
            
            x_lb, x_ulb_w, x_ulb_s = x_lb.cuda(args.gpu), x_ulb_w.cuda(args.gpu), x_ulb_s.cuda(args.gpu)
            y_lb = y_lb.cuda(args.gpu)
            
            inputs = torch.cat((x_lb, x_ulb_w, x_ulb_s))
            
            # inference and calculate losses
            with amp_cm():
                logits, feature = feature_extractor.forward(inputs, ood_test=True) 
                logits_x_lb = logits[:num_lb]
                logits_x_ulb_w, logits_x_ulb_s = logits[num_lb:].chunk(2)
                pseudo_label = torch.softmax(logits_x_ulb_w, dim=-1)

                # for A
                feature, logits_reverse, predict_prob = cls_reverse.forward(feature)
                logits_x_lb_reverse = logits_reverse[:num_lb]
                logits_x_ulb_w_reverse, logits_x_ulb_s_reverse = logits_reverse[num_lb:].chunk(2)

                feature_separate, logits_reverse_separate, predict_prob_separate = cls_reverse.forward(feature[:num_lb])
                pseudo_label_cls_reverse = torch.softmax(logits_x_ulb_w_reverse, dim=-1)

                # construct complementary label
                res = [0] * len(y_lb)
                all_possible_labels = set(range(args.num_classes))
                for each in range(len(res)):
                    potential_anws = all_possible_labels - set([y_lb[each].item()])
                    res[each] = np.random.choice(list(potential_anws))
                res_torch = torch.tensor(res, dtype=y_lb.dtype)
                res_torch = res_torch.cuda()

                distri[count] = pseudo_label.detach().mean(0)
                distri_reverse[count] = pseudo_label_cls_reverse.detach().mean(0)
                count = (count + 1) % 128
               
                del logits

                sup_loss = ce_loss(logits_x_lb, y_lb, reduction='mean')
                reverse_loss = ce_loss(logits_reverse_separate, res_torch, reduction='mean')
    
                unsup_loss_ca, unsup_loss_cd = consistency_loss_bda(
                                              logits_x_ulb_w_reverse,
                                              logits_x_ulb_s_reverse,
                                              logits_x_ulb_w, 
                                              logits_x_ulb_s, 
                                              distri,
                                              distri_reverse)

                total_loss = sup_loss + reverse_loss + self.lambda_u * unsup_loss_cd + self.lambda_u * unsup_loss_ca         
                    
            # parameter updates
            if args.amp:
                scaler.scale(total_loss).backward()
                scaler.step(self.optimizer)
                scaler.update()
            else:
                total_loss.backward() 
                self.optimizer.step()
                
            self.scheduler.step()
            self.train_model.zero_grad()
            
            
            with torch.no_grad():
                self._eval_model_update()
            
            end_run.record()
            torch.cuda.synchronize()
            
            #tensorboard_dict update
            tb_dict = {}
            tb_dict['train/sup_loss'] = sup_loss.detach() 
            tb_dict['train/unsup_loss_cd'] = unsup_loss_cd.detach() 
            tb_dict['train/unsup_loss_ca'] = unsup_loss_ca.detach() 
            tb_dict['train/reverse_loss'] = reverse_loss.detach()
            tb_dict['train/total_loss'] = total_loss.detach() 
            tb_dict['lr'] = self.optimizer.param_groups[0]['lr']
            tb_dict['train/prefecth_time'] = start_batch.elapsed_time(end_batch)/1000.
            tb_dict['train/run_time'] = start_run.elapsed_time(end_run)/1000.
            
            
            if self.it % self.num_eval_iter == 0:
                
                eval_dict = self.evaluate(args=args,lb_loader=self.loader_dict['train_lb'],ulb_loader=self.loader_dict['eval_ulb'])
                tb_dict.update(eval_dict)
                
                save_path = os.path.join(args.save_dir, args.save_name)
                
                if tb_dict['eval/top-1-acc'] > best_eval_acc:
                    best_eval_acc = tb_dict['eval/top-1-acc']
                    best_it = self.it
                
                self.print_fn(f"{self.it} iteration, USE_EMA: {hasattr(self, 'eval_model')}, {tb_dict}, BEST_EVAL_ACC: {best_eval_acc}, at {best_it} iters")
            
            if not args.multiprocessing_distributed or \
                    (args.multiprocessing_distributed and args.rank % ngpus_per_node == 0):
                
                if self.it == best_it:
                    self.save_model('model_best.pth', save_path)
                elif self.it % self.num_eval_iter == 0:
                    self.save_model('latest_model.pth', save_path)
                if not self.tb_log is None:
                    self.tb_log.update(tb_dict, self.it)

            self.it +=1
            del tb_dict
            start_batch.record()
            if self.it > 2**19:
                self.num_eval_iter = 1000
        
        eval_dict = self.evaluate(args=args,lb_loader=self.loader_dict['train_lb'],ulb_loader=self.loader_dict['eval_ulb'])
        eval_dict.update({'eval/best_acc': best_eval_acc, 'eval/best_it': best_it})
        return eval_dict
            
            
    @torch.no_grad()
    def evaluate(self, eval_loader=None, args=None , lb_loader=None ,ulb_loader=None):
        use_ema = hasattr(self, 'eval_model')
        
        eval_model = self.eval_model if use_ema else self.train_model
        eval_model.eval()
        feature_extractor = self.eval_model.module.feature_extractor if hasattr(self.eval_model, 'module') else self.eval_model.feature_extractor
        cls_reverse = self.eval_model.module.classifier_reverse if hasattr(self.eval_model, 'module') else self.eval_model.classifier_reverse
        if eval_loader is None:
            eval_loader = self.loader_dict['eval']
        
        total_loss = 0.0
        total_acc = 0.0
        total_acc_fix = 0.0
        total_num = 0.0
        for x, y in eval_loader:
            y = y.long()
            x, y = x.cuda(args.gpu), y.cuda(args.gpu)

            num_batch = x.shape[0]
            total_num += num_batch

            logits, feature = feature_extractor.forward(x, ood_test=True)         
            max_probs, max_idx = torch.max(torch.softmax(logits, dim=-1), dim=-1)

            feature, logits_reverse, predict_prob = cls_reverse.forward(feature)
            pseudo_label_reverse = torch.softmax(logits_reverse, dim=-1)
            min_probs_reverse, min_idx_reverse = torch.min(pseudo_label_reverse, dim=-1)

            loss = F.cross_entropy(logits, y, reduction='mean')
            acc = torch.sum(max_idx == y)
            acc_fix = torch.sum(min_idx_reverse == y)         
            total_loss += loss.detach()*num_batch
            total_acc += acc.detach()
            total_acc_fix += acc_fix.detach()         

        if not use_ema:
            eval_model.train()         

        acc_p = 0.0
        acc_p_r = 0.0
        totalnum = 0.0
        totalnum_p = 0.0

        for  data in zip(ulb_loader):
            if args.dataset == 'miniimage':
                image, target = data[0][0], data[0][1]
                image = image[0]
            else:
                image, image_s, target, idx = data[0][0], data[0][1], data[0][2], data[0][3]
            
            image = image.type(torch.FloatTensor).cuda()
            num_batch = image.shape[0]
  
            logits, feature = feature_extractor.forward(image, ood_test=True)    
            pseudo_label = torch.softmax(logits, dim=-1)
            max_probs, max_idx = torch.max(pseudo_label, dim=-1)  
            mask = max_probs.ge(0) 

            feature, logits_reverse, predict_prob = cls_reverse.forward(feature)
            pseudo_label_reverse = torch.softmax(logits_reverse, dim=-1)
            max_probs_reverse, max_idx_reverse = torch.max(pseudo_label_reverse, dim=-1)
            min_probs_reverse, min_idx_reverse = torch.min(pseudo_label_reverse, dim=-1)
            maskindex_total = np.where(mask.cpu()==1)[0]
            
            acc_p_r += pseudo_label_reverse.cpu().max(1)[1].eq(target).sum().cpu().numpy()
            acc_p += pseudo_label.cpu().max(1)[1].eq(target).sum().cpu().numpy()              
             
            totalnum += max_probs_reverse.numel()
            totalnum_p += len(maskindex_total)

        pseudo_label_acc=acc_p/totalnum

        if  pseudo_label_acc > self.highest_pseduo_acc :            
            self.highest_pseduo_acc = pseudo_label_acc

        return {'eval/loss': total_loss/total_num, 'eval/top-1-acc': total_acc/total_num , 
                'eval/top-1-acc(argmin->A)': total_acc_fix/total_num ,
                'ulb/pseudo_label_acc':pseudo_label_acc,
                'ulb/complementary_pseudo_label_acc':1-(acc_p_r/totalnum),
                'ulb/highest_pseduo_acc':self.highest_pseduo_acc,
                }
    
    
    def save_model(self, save_name, save_path):
        save_filename = os.path.join(save_path, save_name)
        train_model = self.train_model.module if hasattr(self.train_model, 'module') else self.train_model
        eval_model = self.eval_model.module if hasattr(self.eval_model, 'module') else self.eval_model
        torch.save({'train_model': train_model.state_dict(),
                    'eval_model': eval_model.state_dict(),
                    'optimizer': self.optimizer.state_dict(),
                    'scheduler': self.scheduler.state_dict(),
                    'it': self.it}, save_filename)
        
        self.print_fn(f"model saved: {save_filename}")
    
    
    def load_model(self, load_path):
        checkpoint = torch.load(load_path,map_location=torch.device('cpu'))
        
        train_model = self.train_model.module if hasattr(self.train_model, 'module') else self.train_model
        eval_model = self.eval_model.module if hasattr(self.eval_model, 'module') else self.eval_model
        
        for key in checkpoint.keys():
            if hasattr(self, key) and getattr(self, key) is not None:
                if 'train_model' in key:
                    train_model.load_state_dict(checkpoint[key], strict=False)
                elif 'eval_model' in key:
                    eval_model.load_state_dict(checkpoint[key], strict=False)
                elif key == 'it':
                    self.it = checkpoint[key]
                elif key == 'scheduler':
                    self.scheduler.load_state_dict(checkpoint[key])
                elif key == 'optimizer':
                    self.optimizer.load_state_dict(checkpoint[key]) 
                else:
                    getattr(self, key).load_state_dict(checkpoint[key])
                self.print_fn(f"Check Point Loading: {key} is LOADED")
            else:
                self.print_fn(f"Check Point Loading: {key} is **NOT** LOADED")

if __name__ == "__main__":
    pass
