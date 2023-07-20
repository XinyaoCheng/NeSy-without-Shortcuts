from __future__ import print_function

import argparse
import random
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim import lr_scheduler
import numpy as np
import torchvision
import pickle
import math

from tqdm import tqdm
from torchvision import transforms
from torch.autograd import Variable
from torch.utils.data import DataLoader, Subset
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
import time

import sys
sys.path.append('../../')
from utils import Bar, Logger, AverageMeter, accuracy, mkdir_p, savefig
from logic_encoder import *
import models

import os
os.environ['CUDA_VISIBLE_DEVICES'] = '0, 1'
use_cuda = torch.cuda.is_available()

from sklearn.metrics import confusion_matrix
from torch.autograd import Variable
from torch.utils.data.sampler import SubsetRandomSampler

parser = argparse.ArgumentParser(description='PyTorch CIFAR-100 Logic Training')
parser.add_argument('--seed', default=1, type=int, help='Random seed to use.')
parser.add_argument('--dataset', default='cifar100', type=str, help='Data set.')
parser.add_argument('--net_type', default='resnet50', type=str, help='Model')
parser.add_argument('--num_labeled', default=100, type=int, help='Number of labeled examples (per class!).')
parser.add_argument('--sgd_lr', type=float, default=0.1, help='The learning rate of SGD')
parser.add_argument('--adam_lr', type=float, default=0.001, help='The learning rate of Adam')
parser.add_argument('--exp_name', default='', type=str, help='Experiment name')
parser.add_argument('--resume_from', type=str, default=None, help='Resume from checkpoint')
parser.add_argument('--trun', type=bool, default=False, help='Using truncated gaussian framework')
parser.add_argument('--z_sigma', type=float, default=1, help='The variance of gaussian')
parser.add_argument('--target_sigma', type=float, default=1, help='The lower bound of variance')
parser.add_argument('--constraint', type=bool, default=False, help='Constraint system to use')
parser.add_argument('--constraint_weight', type=float, default=1.0, help='Constraint weight')
parser.add_argument('--tol', type=float, default=1e-2, help='Tolerance for constraints')
args = parser.parse_args()
sys.path.append('../')
from config import *

eps = 0.1

# Hyper Parameter settings
use_cuda = torch.cuda.is_available()
best_acc = 0
best_model = None
start_epoch, num_epochs, batch_size, optim_type = start_epoch, num_epochs, batch_size, optim_type
sgd_lr, adam_lr = args.sgd_lr, args.adam_lr
# Data Uplaod
print('\n[Phase 1] : Data Preparation')
transform_train = transforms.Compose([
        transforms.Resize([32,32]),
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,)), 
    ])

transform_rotate = transforms.Compose([
        transforms.Resize([32,32]),
        transforms.RandomRotation(degrees=[160,200]),
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,)), 
    ])

transform_test = transforms.Compose([
        transforms.Resize([32,32]),
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,)), 
    ])

print("| Preparing MNIST dataset...")
sys.stdout.write("| ")
MNIST = dataset_with_indices(torchvision.datasets.MNIST)
trainset = MNIST(root='../../data/mnist', train=True, download=True, transform=transform_train)
rotateset = MNIST(root='../../data/mnist', train=True, download=True, transform=transform_rotate)
testset = MNIST(root='../../data/mnist', train=False, download=False, transform=transform_test)
num_classes = 10

num_train = len(trainset)

per_class = [[] for _ in range(10)]
for i in range(num_train):
    per_class[trainset[i][1]].append(i)

train_lab_idx = []
train_unlab_idx = []
valid_idx = []
    
np.random.seed(args.seed)
torch.manual_seed(args.seed)
random.seed(args.seed)
if use_cuda:
    torch.cuda.manual_seed(args.seed)
torch.backends.cudnn.deterministic = True
for i in range(10):
    np.random.shuffle(per_class[i])
    split = int(np.floor(0.2 * len(per_class[i])))
    if i != 6:
        train_lab_idx += per_class[i][split:]
    elif i == 6:
        train_unlab_idx += per_class[i][split:]
    valid_idx += per_class[i][:split]

print('Total train[labeled]: ', len(train_lab_idx))
print('Total train[unlabeled]: ', len(train_unlab_idx))
print('Total valid: ', len(valid_idx))
num_cons = len(train_unlab_idx)
underline = np.arange(len(train_lab_idx)+len(train_unlab_idx)+len(valid_idx))
tmp = np.arange(num_cons)
underline[train_unlab_idx] = tmp

train_labeled_sampler = SubsetRandomSampler(train_lab_idx)
train_unlabeled_sampler = SubsetRandomSampler(train_unlab_idx)
valid_sampler = SubsetRandomSampler(valid_idx)

unlab_batch = batch_size if args.constraint != 'none' else 1

trainloader_lab = torch.utils.data.DataLoader(
    trainset, batch_size=batch_size, sampler=train_labeled_sampler, num_workers=2)
trainloader_unlab = torch.utils.data.DataLoader(
    trainset, batch_size=unlab_batch, sampler=train_unlabeled_sampler, num_workers=2)
validloader = torch.utils.data.DataLoader(
    trainset, batch_size=batch_size, sampler=valid_sampler, num_workers=2)

def getNetwork(args):
    if args.net_type == 'lenet':
        net = models.LeNet(10)
        file_name = 'lenet'
    elif args.net_type == 'mlp':
        net = models.MLP(10)
        file_name = 'mlp'
    else:
        assert False
    file_name += '_' + str(args.seed) + '_' + args.exp_name
    return net, file_name

# cons operator
def initial_constriants():
    var_or = [None for i in range(num_cons)] # not a good method, update later
    var_and = [None for i in range(num_cons)] # not a good method, update later
    net.eval()

    for batch_idx, ulab in enumerate(trainloader_unlab):
        inputs_u, targets_u, index = ulab
        index = underline[index]
        inputs_u, targets_u = Variable(inputs_u), Variable(targets_u)
        n_u = inputs_u.size()[0]
        if use_cuda:
            inputs_u, targets_u = inputs_u.cuda(), targets_u.cuda() # GPU settings

        outputs = net(inputs_u)
        probs_u = softmax(outputs)        
        
        # constraint_loss = 0
        if args.constraint == True:
            for k in range(n_u):
                # or_res1 = Or([LE(probs_u[k,6]-probs_u[k,i], 0.0) for i in [0,1,2,3,4,5,7,8,9]])
                # l1 = [GE(probs_u[k,6]-probs_u[k,i], 0.0) for i in [0,1,2,3,4,5,7,8,9]]
                # l2 = [GE(probs_u[k,9]-probs_u[k,i], 0.0) for i in [0,1,2,3,4,5,6,7,8]]
                # and_res2 = And(l1 + l2)
                # 
                # or_res1 = Or([LE(probs_u[k,9]-probs_u[k,i], 0.0) for i in [0,1,2,3,4,5,6,7,8]])
                # and_res2 = And([GE(probs_u[k,6]-probs_u[k,i], 0.0) for i in [0,1,2,3,4,5,7,8,9]])
                #
                or_res1 = Or([EQ(probs_u[k,i], 0.0) for i in [0,1,2,3,4,5,6,7,8]])
                and_res2 = EQ(probs_u[k,6], 1.0)
                if var_and[index[k]] is None:
                    var_or[index[k]] = or_res1.tau.numpy()
                    # var_and[index[k]] = and_res2.tau.numpy()
                else:
                    var_or[index[k]] = np.append(var_or[index[k]], or_res1.tau.numpy())
                    # var_and[index[k]] = np.append(var_and[index[k]], and_res2.tau.numpy())
                or_res = Or([or_res1, and_res2])
                var_or[index[k]] = np.append(var_or[index[k]], or_res.tau.numpy())
    return var_or, var_and

def cons_loss(probs_u, probs_r, index, n_u):
    constraint_loss = 0
    cons = []
    # encoding type 1
    # or_res1 = BatchOr([LE(probs_u[:,6]-probs_u[:,i], -0.01) for i in [0,1,2,3,4,5,7,8,9]], index.shape[0], var_or[index,0:9])
    # l1 = [GE(probs_u[:,6]-probs_u[:,i], 0.01) for i in [0,1,2,3,4,5,7,8,9]]
    # l2 = [GE(probs_r[:,9]-probs_r[:,i], 0.01) for i in [0,1,2,3,4,5,6,7,8]]
    # and_res2 = BatchAnd(l1 + l2, batch_size, var_and[index,0:18])
    
    # encoding type 2
    # or_res1 = BatchOr([LE(probs_r[:,9]-probs_r[:,i], -eps) for i in [0,1,2,3,4,5,6,7,8]], index.shape[0], var_or[index,0:9])
    # and_res2 = BatchAnd([GE(probs_u[:,6]-probs_u[:,i], eps) for i in [0,1,2,3,4,5,7,8,9]], index.shape[0], var_and[index,0:9])
    
    # encoding type 3 
    or_res1 = BatchOr([EQ(probs_r[:,i], 1.0) for i in [0,1,2,3,4,5,6,7,8]], index.shape[0], var_or[index,0:9])
    and_res2 = EQ(probs_u[:,6], 1.0)
    or_res = BatchOr([or_res1, and_res2], index.shape[0], var_or[index, 9:11])
    
    hwx_loss = or_res.encode()
    if args.trun == True: 
        # maximum likelihood of truncated gaussians
        xi = (0 - hwx_loss) / args.z_sigma
        over = - 0.5 * xi.square() 
        tmp = torch.erf(xi / np.sqrt(2))
        under = torch.log(1 - tmp) 
        loss = -(over - under).mean()
        constraint_loss += loss     
    else:
        constraint_loss += hwx_loss.square().mean() / np.square(args.target_sigma)
    return constraint_loss, hwx_loss.detach().cpu().numpy()


# Model
print('\n[Phase 2] : Model setup')
if args.resume_from is not None:
    # Load checkpoint
    print('| Resuming from checkpoint...')
    assert os.path.isdir('checkpoint'), 'Error: No checkpoint directory found!'
    _, file_name = getNetwork(args)
    checkpoint = torch.load('./checkpoint/' + args.resume_from + '.t7')
    net = checkpoint['net']
    best_acc = checkpoint['acc']
    start_epoch = checkpoint['epoch']             
else:
    print('| Building net type [' + args.net_type + ']...')
    net, file_name = getNetwork(args)
    # net.apply(conv_init)

if use_cuda:
    net.cuda()
    net = torch.nn.DataParallel(net, device_ids=range(torch.cuda.device_count()))
    cudnn.benchmark = True   

if args.resume_from is not None:
    if args.constraint == True:
        var_and = checkpoint['tau_and']
        var_or  = checkpoint['tau_or']
    else:
        var_or = None
        var_and = None
        print('\n| initial constraints')
        print('| No constraints are encoded') 
    print('\n| load constraints')
    print('| Total disjunctive constraints = ' + str(var_or.shape[0]))
    print('| Total conjunective constraints = ' + str(var_and.shape[0]))                 
else:
    if args.constraint == True:
        var_or, var_and = initial_constriants()
        if use_cuda:
            # device = torch.device('cuda')
            var_or = torch.tensor(var_or, requires_grad=True, device='cuda')
            # var_and = torch.tensor(var_and, requires_grad=True, device='cuda')
            sigma = torch.tensor(args.z_sigma, device='cuda')
        else:
            var_or = torch.tensor(var_or, requires_grad=True)
            var_and = torch.tensor(var_and, requires_grad=True)
            sigma = torch.Tensor(args.z_sigma)
        print('\n| initial constraints')
        print('| Total disjunctive constraints = ' + str(var_or.shape[0]))
        # print('| Total conjunective constraints = ' + str(var_and.shape[0]))          

if args.constraint == False:
    var_or = None
    var_and = None
    print('\n| initial constraints')
    print('| No constraints are encoded')  

criterion = nn.CrossEntropyLoss()
        
# Training
def train(epoch):
    net.train()
    train_loss = 0
    correct = 0
    total = 0
    list_hwx = []

    if args.constraint == True:
        global var_or, var_and, sigma
    softmax = torch.nn.Softmax(dim=1)  

    print('\n=> Training Epoch #%d, LR=%.4f' %(epoch, optim_w.state_dict()['param_groups'][0]['lr'])) # lr 
    for batch_idx, (lab, ulab) in enumerate(zip(trainloader_lab, trainloader_unlab)):
        inputs_u, targets_u, index = ulab
        inputs_u, targets_u = Variable(inputs_u), Variable(targets_u)
        inputs_r = [rotateset[i][0] for i in index]
        inputs_r = torch.cat(inputs_r, dim=0).unsqueeze(1)
        inputs_r = Variable(inputs_r)
        n_u = inputs_u.size()[0]
        index = underline[index]
        if use_cuda:
            inputs_u = inputs_u.cuda() # GPU settings
            inputs_r = inputs_r.cuda()

        if lab is None:
            n = 0
            all_outputs = net(inputs_u, inputs_r)
        else:
            inputs, targets, _ = lab
            inputs, targets = Variable(inputs), Variable(targets)
            n = inputs.size()[0]
            if use_cuda:
                inputs, targets = inputs.cuda(), targets.cuda() # GPU settings
            all_outputs = net(torch.cat([inputs, inputs_u, inputs_r], dim=0))

        outputs_u = all_outputs[n:n+n_u,]
        probs_u = softmax(outputs_u)
        outputs_r = all_outputs[n+n_u:,]
        probs_r = softmax(outputs_r)

        # updates tau
        if args.constraint == True:
            # optim_tau.zero_grad()
            temp_u = probs_u.clone().detach()
            temp_r = probs_r.clone().detach()
            constraint_loss, _ = cons_loss(temp_u, temp_r, index, n_u)
            constraint_loss.backward()
            with torch.no_grad():
                var_or = var_or - tau_lr * var_or.grad
                # var_and = var_and + tau_lr * var_and.grad
            var_or.requires_grad = True
            # var_and.requires_grad = True
        else:
            constraint_loss = 0    

        optim_w.zero_grad()
        # update w
        outputs = all_outputs[:n,]
        ce_loss = criterion(outputs, targets)  # Loss
        
        if args.constraint == True:
            constraint_loss, hwx_loss = cons_loss(probs_u, probs_r, index, n_u)
            list_hwx.extend(hwx_loss)
            loss = ce_loss + args.constraint_weight * constraint_loss
        else:
            loss = ce_loss
        loss.backward()  # Backward Propagation
        optim_w.step() # Optimizer update

        # estimation
        train_loss += loss.item()
        _, predicted = torch.max(outputs.data, 1)
        total += targets.size(0)
        correct += predicted.eq(targets.data).cpu().sum()
        
        sys.stdout.write('\r')
        sys.stdout.write('| Epoch [%3d/%3d] Iter[%3d/%3d]\t\tCE Loss: %.4f, Constraint Loss: %.4f Acc@1: %.3f%%'
                %(epoch, num_epochs, batch_idx+1,
                    (len(train_lab_idx)//batch_size)+1, ce_loss, constraint_loss, 100.*float(correct)/total))
        test(epoch,batch_idx);
        sys.stdout.flush()

    if scheduler is not None:
        scheduler.step()

    # update sigma
    if args.constraint == True:
        tmp_or = np.array(var_or.clone().cpu().detach().numpy())
        print('\n')
        error = np.mean(np.array(list_hwx).reshape(-1))
        sigma = torch.tensor(np.square(error))
        sigma = torch.clamp(sigma, min=args.target_sigma, max=args.z_sigma)
        args.z_sigma = sigma
        print('\n Logic Error: %.3f, Update sigma: %.2f' %(error, sigma.detach().cpu().numpy()))

    return 100.*float(correct)/total

def save(acc, e, net, tau, best=False):
    tau_and, tau_or = tau
    state = {
            'net': net.module if use_cuda else net,
            'acc': acc,
            'epoch': epoch,
            'tau_and': tau_and,
            'tau_or': tau_or,
    }
    if not os.path.isdir('checkpoint'):
        os.mkdir('checkpoint')
    if best:
        e = int(400* math.ceil(( float(epoch) / 400)) )
        save_point = './checkpoint/' + file_name + '_' + str(e) + '_best' + '.t7'
        # save_point = './checkpoint/' + file_name + '_overall_' + '.t7'
    else:
        save_point = './checkpoint/' + file_name + '_' + str(e) + '_' + '.t7'
    torch.save(state, save_point)
    return net, tau
    
def cons_sat(probs_u, probs_r):
    batch_size = probs_u.shape[0]
    or_res1 = BatchOr([LE(probs_r[:,9]-probs_r[:,i], -eps) for i in [0,1,2,3,4,5,6,7,8]], batch_size)
    and_res2 = BatchAnd([GE(probs_u[:,6]-probs_u[:,i], eps) for i in [0,1,2,3,4,5,7,8,9]], batch_size)
    ans_sat1 = or_res1.satisfy(args.tol)
    ans_sat2 = and_res2.satisfy(args.tol)
    return ans_sat1, ans_sat2

def test(epoch,batch_idx):
        global best_acc, best_model, best_tau, best_epoch
        net.eval()
        test_loss = 0
        correct = 0
        constraint_correct1 = 0
        constraint_correct2 = 0
        constraint_num = 0
        total = 0
        for batch_idx, (inputs, targets, index) in enumerate(validloader):
            if use_cuda:
                inputs, targets = inputs.cuda(), targets.cuda()
            inputs, targets = Variable(inputs), Variable(targets)
            outputs = net(inputs)
            loss = criterion(outputs, targets)
            probs = softmax(outputs)

            ind = np.where(targets.cpu().detach().numpy() == 6)[0]
            if len(ind) != 0:
                probs_u = probs[ind, :]
                index = index[ind]
                inputs_r = [rotateset[i][0] for i in index]
                inputs_r = torch.cat(inputs_r, dim=0).unsqueeze(1)
                outputs_r = net(inputs_r)
                probs_r = softmax(outputs_r)
                ans_sat1, ans_sat2 = cons_sat(probs_u, probs_r)
                constraint_correct1 += ans_sat1.sum()
                constraint_correct2 += ans_sat2.sum()
                constraint_num += len(ind)

            test_loss += loss.item()
            _, predicted = torch.max(outputs.data, 1)
            total += targets.size(0)
            correct += predicted.eq(targets.data).cpu().sum()

        # Save checkpoint when best model
        acc = 100. * float(correct) / total
        cons_acc1 = 100. * float(constraint_correct1) / constraint_num
        cons_acc2 = 100. * float(constraint_correct2) / constraint_num
        total_acc = (acc)
        print("\n| Validation Epoch #%d\t\t\tLoss: %.4f Acc@1: %.2f%% Cons_Acc1/2: %.2f%%/%.2f%%" % (
        epoch, loss.item(), acc, cons_acc1, cons_acc2))

        if total_acc > best_acc:
            # print('| Saving Best model...\t\t\tTop1 = %.2f%%' %(acc))
            best_model, best_tau = save(acc, _, net, [var_and, var_or], best=True)
            best_epoch = epoch
            best_acc = total_acc

        # record the result
        with open("./log/my_log_{!s}.txt".format(file_name), "a") as f:
            f.write("\nepoch #%d\titeration #%d\t\t\tLoss: %.4f Acc@1: %.2f%% Cons_Acc1/2: %.2f%%/%.2f%%" % (
            epoch, batch_idx, loss.item(), acc, cons_acc1, cons_acc2))
# def test(epoch):
#     global best_acc, best_model, best_tau, best_epoch
#     net.eval()
#     test_loss = 0
#     correct = 0
#     constraint_correct1 = 0
#     constraint_correct2 = 0
#     constraint_num = 0
#     total = 0
#     for batch_idx, (inputs, targets, index) in enumerate(validloader):
#         if use_cuda:
#             inputs, targets = inputs.cuda(), targets.cuda()
#         inputs, targets = Variable(inputs), Variable(targets)
#         outputs = net(inputs)
#         loss = criterion(outputs, targets)
#         probs = softmax(outputs)
#
#         ind = np.where(targets.cpu().detach().numpy() == 6)[0]
#         if len(ind) != 0:
#             probs_u = probs[ind,:]
#             index = index[ind]
#             inputs_r = [rotateset[i][0] for i in index]
#             inputs_r = torch.cat(inputs_r, dim=0).unsqueeze(1)
#             outputs_r = net(inputs_r)
#             probs_r = softmax(outputs_r)
#             ans_sat1, ans_sat2 = cons_sat(probs_u, probs_r)
#             constraint_correct1 += ans_sat1.sum()
#             constraint_correct2 += ans_sat2.sum()
#             constraint_num += len(ind)
#
#         test_loss += loss.item()
#         _, predicted = torch.max(outputs.data, 1)
#         total += targets.size(0)
#         correct += predicted.eq(targets.data).cpu().sum()
#
#     # Save checkpoint when best model
#     acc = 100.*float(correct)/total
#     cons_acc1 = 100.*float(constraint_correct1)/constraint_num
#     cons_acc2 = 100.*float(constraint_correct2)/constraint_num
#     total_acc = (acc)
#     print("\n| Validation Epoch #%d\t\t\tLoss: %.4f Acc@1: %.2f%% Cons_Acc1/2: %.2f%%/%.2f%%" %(epoch, loss.item(), acc, cons_acc1, cons_acc2))
#
#     if total_acc > best_acc:
#         # print('| Saving Best model...\t\t\tTop1 = %.2f%%' %(acc))
#         best_model, best_tau = save(acc, _, net, [var_and, var_or], best=True)
#         best_epoch = epoch
#         best_acc = total_acc
#
#     # record the result
#     with open("./log/log_{!s}.txt".format(file_name),"a") as f:
#         f.write("\n| Epoch #%d\t\t\tLoss: %.4f Acc@1: %.2f%% Cons_Acc1/2: %.2f%%/%.2f%%" %(epoch, loss.item(), acc, cons_acc1, cons_acc2))


elapsed_time = 0
print('\n[Phase 3] : Training model')
print('| Training Epochs = ' + str(num_epochs))
print('| Initial Learning Rate: sgd_lr = %.4f adam_lr = %.4f' %(sgd_lr, adam_lr))
print('| Optimizer = ' + str(optim_type))
print('| Logical Constraint = ' + str(args.constraint))
print('| Whether truncate = ' + str(args.trun))

# record the result
with open("./log/log_{!s}.txt".format(file_name),"w") as f: 
    f.write("Iteration: sgd_lr = %.4f adam_lr = %.4f" %(sgd_lr, adam_lr))

for epoch in range(start_epoch, start_epoch+num_epochs):
    if epoch == start_epoch and sgd_epochs != 0:
        lr = sgd_lr
        optim_w = optim.SGD(net.parameters(), lr=lr)
        scheduler = None
    elif epoch == start_epoch + sgd_epochs:
        lr = adam_lr
        optim_w = optim.Adam(net.parameters(), lr=lr)
        scheduler = None
        # update tau_lr
        tau_lr = lr_adapt(tau_lr, epoch)
    
    start_time = time.time()

    acc = train(epoch)
    if epoch % 400 == 0:
        save(acc, epoch, net, [var_and, var_or])           
    test(epoch)

    epoch_time = time.time() - start_time
    elapsed_time += epoch_time
    print('| Elapsed time : %d:%02d:%02d'  %(get_hms(elapsed_time)))

if best_model is not None:
    print('The overall best model is from epoch %02d-th' %(best_epoch))
    # save(best_acc, 'overall',  best_model, best_tau)
    
print('\n[Phase 4] : Testing the best model which derived from epoch %02d-th' %(best_epoch))
print('* Val results : Acc@1 = %.2f%%' %(best_acc))
