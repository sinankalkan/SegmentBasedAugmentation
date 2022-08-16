# LIBRARIES #
import warnings
from pathlib import Path
import os
import sys
import numpy as np
import argparse
import datetime
import time
import pickle as pkl
import random
import json
import matplotlib
import matplotlib.pyplot as plt
from tqdm import tqdm
import pandas as pd
import torch
import torch.nn as nn
import auxiliaries_nofaiss as aux
import datasets as data
import netlib as netlib
import losses as losses
import evaluate as eval

warnings.filterwarnings("ignore")
os.chdir(os.path.dirname(os.path.realpath(__file__)))
matplotlib.use('agg')
torch.backends.cudnn.enabled = False
# INPUT ARGUMENTS #
parser = argparse.ArgumentParser()

# Main Parameter: Dataset to use for Training
parser.add_argument('--dataset', default='METU-Trademark',   type=str, help='Dataset to use.')

# General Training Parameters
parser.add_argument('--lr', default=0.0000001, type=float, help='Learning Rate for network parameters.')
parser.add_argument('--fc_lr_mul', default=5, type=float, help='OPTIONAL: Multiply the embedding layer learning rate'
                                                               ' by this value.''If set to 0, the embedding layer '
                                                               'shares the same learning rate.')
parser.add_argument('--n_epochs', default=50, type=int, help='Number of training epochs.')
parser.add_argument('--kernels', default=0, type=int, help='Number of workers for pytorch dataloader.')
parser.add_argument('--bs', default=256, type=int, help='Mini-Batchsize to use.')
parser.add_argument('--samples_per_class', default=4, type=int, help='Number of samples in one class drawn before '
                                                                     'choosing the next class. Set to >1 for losses'
                                                                     ' other than ProxyNCA.')
parser.add_argument('--seed', default=1, type=int, help='Random seed for reproducibility.')
parser.add_argument('--scheduler', default='step', type=str, help='Type of learning rate scheduling.'
                                                                  ' Currently: step & exp.')
parser.add_argument('--gamma', default=0.3, type=float, help='Learning rate reduction after tau epochs.')
parser.add_argument('--decay', default=0.0004, type=float, help='Weight decay for optimizer.')
parser.add_argument('--tau', default=[200, 300, 300, 120, 220, 250, 280], nargs='+', type=int, help='Stepsize(s) before reducing learning rate.')
parser.add_argument('--opt', default='adam', help='adam or sgd')

# Loss-specific Settings
parser.add_argument('--loss', default='smoothap', type=str, help='loss options: smoothap, tripletloss')
parser.add_argument('--sampling', default='distance', type=str, help='For triplet-based losses:'
                                                                     ' Modes of Sampling: random, semihard, distance.')
parser.add_argument('--sigmoid_temperature', default=0.01, type=float, help='SmoothAP: the temperature of the sigmoid used in SmoothAP loss')
parser.add_argument('--margin', default=0.2, type=float, help='TRIPLET/MARGIN: Margin for Triplet-based Losses')

# Evaluation Settings
parser.add_argument('--k_vals', nargs='+', default=[1, 2, 4, 8], type=int, help='Recall @ Values.')

# Network parameters
parser.add_argument('--embed_dim', default=512, type=int, help='Embedding dimensionality of the network. '
                                                               'Note: in literature, dim=128 is used for ResNet50 and'
                                                               ' dim=512 for GoogLeNet.')
parser.add_argument('--embed_init', default='default', type=str, help='Embedding layer initialization method:'
                                                                      '{default,kaiming_normal,kaiming_uniform,normal}')
parser.add_argument('--arch', default='convnext', type=str, help='Network backend choice: resnet50, googlenet.')
parser.add_argument('--resize256', action='store_true', help='If added, resize training images to 256x256 first.')
parser.add_argument('--ft_batchnorm', action='store_true', help='If added, BatchNorm layers will '
                                                                'be un-frozen for finetuning.')
parser.add_argument('--not_pretrained', action='store_true', help='If added, the network will be trained'
                                                                  ' WITHOUT ImageNet-pretrained weights.')
parser.add_argument('--grad_measure', action='store_true', help='If added, gradients passed from embedding layer'
                                                                ' to the last conv-layer are stored in each iteration.')
parser.add_argument('--dist_measure', action='store_true', help='If added, the ratio between intra- and interclass'
                                                                ' distances is stored after each epoch.')

# Setup Parameters
parser.add_argument('--init_pth', default=None, type=str)
parser.add_argument('--eval_only', action='store_true', help='If added, only evaluate model.')
parser.add_argument('--gpu', default=0, type=int, help='GPU-id for GPU to use.')
parser.add_argument('--savename', default='', type=str, help='Save folder name if any special'
                                                             ' information is to be included.')

# Paths to datasets and storage folder
parser.add_argument('--source_path', default='./Datasets', type=str, help='Path to training data.')
parser.add_argument('--save_path', default='./Training_Results', type=str, help='Where to save everything.')

# Read in parameters
opt = parser.parse_args()
opt.source_path += '/'+opt.dataset
opt.save_path += '/'+opt.dataset

opt.pretrained = not opt.not_pretrained
opt.eval_only = 0

timestamp = datetime.datetime.now().strftime(r"%Y-%m-%d_%H-%M-%S")
exp_name = aux.args2exp_name(opt)
opt.save_name = f"weights_{exp_name}" +'/'+ timestamp
random.seed(opt.seed)
np.random.seed(opt.seed)
torch.manual_seed(opt.seed)
torch.cuda.manual_seed(opt.seed); torch.cuda.manual_seed_all(opt.seed)

"""============================================================================"""
# GPU SETTINGS #
os.environ["CUDA_VISIBLE_DEVICES"] = str(opt.gpu)

"""============================================================================"""
# SEEDS FOR REPROD.#
torch.backends.cudnn.deterministic = True
np.random.seed(opt.seed); random.seed(opt.seed)
torch.manual_seed(opt.seed)

"""============================================================================"""
# NETWORK SETUP #
opt.device = torch.device('cuda')
# Depending on the choice opt.arch, networkselect() returns the respective network model
model = netlib.networkselect(opt)

print('{} Setup for {} with {} sampling on {} complete with #weights: {}'.format(opt.loss.upper(), opt.arch.upper(),
                                                                                 opt.sampling.upper(),
                                                                                 opt.dataset.upper(),
                                                                                 aux.gimme_params(model)))
# Push to Device
_ = model.to(opt.device)
# Place trainable parameter in list of parameters to train:
# if 'fc_lr_mul' in vars(opt).keys() and opt.fc_lr_mul!=0:
#    all_but_fc_params = filter(lambda x: 'last_linear' not in x[0],model.named_parameters())
#    fc_params         = model.model.last_linear.parameters()
#    to_optim          = [{'params':all_but_fc_params,'lr':opt.lr,'weight_decay':opt.decay},
#                         {'params':fc_params,'lr':opt.lr*opt.fc_lr_mul,'weight_decay':opt.decay}]
#    print(to_optim)
#    pdb.set_trace()
# else:
#    to_optim   = [{'params':model.parameters(),'lr':opt.lr,'weight_decay':opt.decay}]
to_optim = model.to_optim(opt)
print(model)
#to_optim = [{'params':model.parameters(),'lr':opt.lr,'weight_decay':opt.decay}]

"""============================================================================"""
# DATALOADER SETUPS #
# Returns a dictionary containing 'training', 'testing', and 'evaluation' dataloaders.
# The 'testing'-dataloader corresponds to the validation set, and the 'evaluation'-dataloader
# Is simply using the training set, however running under the same rules as 'testing' dataloader,
# i.e. no shuffling and no random cropping.
dataloaders = data.give_dataloaders(opt)
# Because the number of supervised classes is dataset dependent, we store them after
# initializing the dataloader
opt.num_classes = len(dataloaders['training'].dataset.avail_classes)

"""============================================================================"""
# CREATE LOGGING FILES #
# Each dataset usually has a set of standard metrics to log. aux.metrics_to_examine()
# returns a dict which lists metrics to log for training ('train') and validation/testing ('val')

metrics_to_log = aux.metrics_to_examine(opt.dataset, opt.k_vals)
# example output: {'train': ['Epochs', 'Time', 'Train Loss', 'Time'],
#                  'val': ['Epochs','Time','NMI','F1', 'Recall @ 1','Recall @ 2','Recall @ 4','Recall @ 8']}

# Using the provided metrics of interest, we generate a LOGGER instance.
# Note that 'start_new' denotes that a new folder should be made in which everything will be stored.
# This includes network weights as well.
LOG = aux.LOGGER(opt, metrics_to_log, name='Base', start_new=True)

"""============================================================================"""
# OPTIONAL EVALUATIONS #
# Store the averaged gradients returned from the embedding to the last conv. layer.
if opt.grad_measure:
    grad_measure = eval.GradientMeasure(opt, name='baseline')
# Store the relative distances between average intra- and inter-class distance.
    # Add a distance measure for training distance ratios
distance_measure = eval.DistanceMeasure(dataloaders['evaluation'], opt, name='Train', update_epochs=1)
    # If uncommented: Do the same for the test set
#distance_measure_test = eval.DistanceMeasure(dataloaders['query'], opt, name='Train', update_epochs=1)
"""============================================================================"""
# LOSS SETUP #
# Depending on opt.loss and opt.sampling, the respective criterion is returned,
# and if the loss has trainable parameters, to_optim is appended.
criterion, to_optim = losses.loss_select(opt.loss, opt, to_optim)
_ = criterion.to(opt.device)
"""============================================================================"""
# OPTIM SETUP #
# As optimizer, Adam with standard parameters is used.
optimizer = torch.optim.Adam(to_optim)

if opt.scheduler == 'exp':
    scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=opt.gamma)
elif opt.scheduler == 'step':
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=opt.tau, gamma=opt.gamma)
elif opt.scheduler == 'none':
    print('No scheduling used!')
else:
    raise Exception('No scheduling option for input: {}'.format(opt.scheduler))
"""============================================================================"""
# TRAINER FUNCTION #


def train_one_epoch(train_dataloader, model, optimizer, criterion, opt, epoch):
    """
    This function is called every epoch to perform training of the network over one full
    (randomized) iteration of the dataset
    Args:
        train_dataloader: torch.utils.data.DataLoader, returns (augmented) training data.
        model:            Network to train.
        optimizer:        Optimizer to use for training.
        criterion:        criterion to use during training.
        opt:              argparse.Namespace, Contains all relevant parameters.
        epoch:            int, Current epoch.
    Returns:
        Nothing!
    """

    loss_collect = []
    start = time.time()
    data_iterator = tqdm(train_dataloader, desc='Epoch {} Training...'.format(epoch))
    for i, (class_labels, inputs) in enumerate(data_iterator):
        # Compute embeddings for input batch.
        features = model(inputs.to(opt.device))
        # Compute loss.
        if opt.loss == "smoothap":
            loss = criterion(features)
        else:
            loss = criterion(features, class_labels)
        # Compute gradients.
        optimizer.zero_grad()

        loss.backward()
        if opt.grad_measure:
            # If desired, save computed gradients.
            grad_measure.include(model.model.last_linear)

        # Update weights using comp. gradients.
        optimizer.step()

        # Store loss per iteration.
        loss_collect.append(loss.item())
        if i == len(train_dataloader)-1:
            data_iterator.set_description('Epoch (Train) {0}: Mean Loss [{1:.4f}]'.format(epoch, np.mean(loss_collect)))
    # Save metrics
    LOG.log('train', LOG.metrics_to_log['train'], [epoch, np.round(time.time()-start, 4), np.mean(loss_collect)])
    if opt.grad_measure:
        # Dump stored gradients to Pickle-File.
        grad_measure.dump(epoch)


"""============================================================================"""
"""========================== MAIN TRAINING PART =============================="""
"""============================================================================"""
# SCRIPT MAIN #
print('\n-----\n')
if opt.eval_only:
    opt.n_epochs = 1
    print("Evaluation-only mode!")

for epoch in range(opt.n_epochs):
    if epoch % 3 == 0:
        print(f"GPU:{opt.gpu}, dataset:{opt.dataset}, arch:{opt.arch}, embed_dim:{opt.embed_dim},"
              f" embed_init:{opt.embed_init}")
        print(f"loss:{opt.loss}, sampling:{opt.sampling}, samples_per_class:{opt.samples_per_class}, "
              f"resize256:{opt.resize256}")
        print(f"bs:{opt.bs}, lr:{opt.lr}, fc_lr_mul:{opt.fc_lr_mul}, decay:{opt.decay}, "
              f"gamma:{opt.gamma}, tau:{opt.tau}, bnft:{opt.ft_batchnorm}")

    # Print current learning rates for all parameters
    if not opt.eval_only and opt.scheduler != 'none':
        print('Running with learning rates {}...'.format(' | '.join('{}'.format(x) for x in scheduler.get_lr())))

    # Train one epoch
    if not opt.eval_only:
        _ = model.train()
        train_one_epoch(dataloaders['training'], model, optimizer, criterion, opt, epoch)

    # (optional) compute ratio of intra- to interdistances.
    # distance_measure.measure(model, epoch)
    # distance_measure_test.measure(model, epoch)

    ### Learning Rate Scheduling Step
    if not opt.eval_only and opt.scheduler != 'none':
       scheduler.step()

        ### Evaluate
    _ = model.eval()

    # Update the Metric Plot and save it.
    # LOG.update_info_plot()

    # Each dataset requires slightly different dataloaders.
    eval_params = {'query_dataloader': dataloaders['query'], 'gallery_dataloader': dataloaders['testing_gallery'],
                    'model': model, 'opt': opt, 'epoch': epoch}

    # Compute Evaluation metrics, print them and store in LOG.
    results = eval.evaluate(LOG, save=True, **eval_params)






