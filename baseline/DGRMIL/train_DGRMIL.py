# -*- coding: utf-8 -*-
"""
Created on Tue Sep 12 19:34:41 2023

@author: Xiwen Chen
"""

# -*- coding: utf-8 -*-
"""
Created on Sun Sep 10 19:25:56 2023

@author: Xiwen Chen
"""

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import sys, argparse, copy
import numpy as np
#from tqdm import tqdm
from sklearn.metrics import roc_curve, roc_auc_score,f1_score,recall_score,precision_score
from dataset.wsi_dataloader_3 import C16DatasetV3
from torch.cuda.amp import GradScaler, autocast
import torch.nn.functional as F
import warnings
from models.dgrmil import DGRMIL
from dataset.base import *
from config import *
from utils import *

 

# Suppress all warnings
warnings.filterwarnings("ignore")

 
from scheduler import LinearWarmupCosineAnnealingLR                                    
                                        
                                        
def train(trainloader, milnet, criterion, optimizer, epoch, start,args,scaler):
    milnet.train()
    total_loss = 0
    for batch_id, (feats,label) in enumerate(trainloader):
        bag_feats = feats.cuda()
        bag_label = label.cuda()
        optimizer.zero_grad()

        with autocast():
            if torch.argmax(label)==0:
                bag_prediction, A,H,p_center,nc_center,lesion = milnet(bag_feats,bag_mode='normal')
             
            else:
                bag_prediction, A,H,p_center,nc_center,lesion= milnet(bag_feats,bag_mode='abnormal')

            bag_loss = criterion(bag_prediction.view(1, -1), bag_label.view(1, -1))

            if epoch<args.epoch_des:
                loss = bag_loss 
                sys.stdout.write('\r Training bag [%d/%d] bag loss: %.4f  total loss: %.4f' % \
                            (batch_id, len(trainloader), bag_loss.item(),loss.item()))
            else:
                lesion_norm = lesion.squeeze(0)
                lesion_norm = torch.nn.functional.normalize(lesion_norm)
                div_loss = -torch.logdet(lesion_norm@lesion_norm.T+1e-10*torch.eye(args.num_les).cuda())
                #print(div_loss)
                sim_loss = tripleloss(lesion,p_center,nc_center)
                loss = bag_loss  +   0.1*div_loss + 0.1*sim_loss  
                sys.stdout.write('\r Training bag [%d/%d] bag loss: %.4f   sim_loss: %.4f  div loss: %.4f  total loss: %.4f' % \
                            (batch_id, len(trainloader), bag_loss.item(),sim_loss.item(),div_loss.item(),loss.item()))
                
        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()

        total_loss = total_loss + bag_loss
 
    return total_loss / len(trainloader)

def similarityloss(golabal,p_center,nc_center):
    golabal = golabal.squeeze(0)
    n_globallesionrepresente, _ = golabal.shape

    p_center = p_center.repeat(n_globallesionrepresente, 1)
    nc_center = nc_center.repeat(n_globallesionrepresente, 1)

    cosin = nn.CosineSimilarity()

    loss_nc = cosin(golabal,nc_center).mean()
     
    loss_p = -cosin(golabal,p_center).mean()
    #print(loss_p)
    totall_loss = loss_nc + loss_p
    
    return totall_loss


def tripleloss(golabal,p_center,nc_center):
    golabal = golabal.squeeze(0)
    n_globallesionrepresente, _ = golabal.shape
    p_center = p_center.repeat(n_globallesionrepresente, 1)
    nc_center = nc_center.repeat(n_globallesionrepresente, 1)

    triple_loss = nn.TripletMarginWithDistanceLoss(distance_function=lambda x, y: 1.0 - F.cosine_similarity(x, y) ,margin=1)
    
    loss = triple_loss(golabal,p_center,nc_center)

    return loss
def test_main(dataname,seed):
    args = get_config(dataname)
    args.dataname = dataname
    args.seed = seed
    set_data(args)
    set_seed(seed)

    # <------------- set up logging ------------->
    logging_path = os.path.join(args.save_dir, 'Train_log.log')
    logger = get_logger(logging_path)

    # <------------- save hyperparams ------------->
    option = vars(args)
    file_name = os.path.join(args.save_dir, 'option.txt')
    with open(file_name, 'wt') as opt_file:
        opt_file.write('------------ Options -------------\n')
        for k, v in sorted(option.items()):
            opt_file.write('%s: %s\n' % (str(k), str(v)))
        opt_file.write('-------------- End ----------------\n')


    # <------------- define MIL network ------------->
    milnet = DGRMIL(args.feats_size, L=args.L, n_lesion=args.num_les, dropout_node=args.dropout_node,
                    dropout_patch=args.dropout_patch,num_classes=args.num_classes).cuda()

    ckp=torch.load(args.ckp,map_location="cuda")
    milnet.load_state_dict(ckp)

    testset = C16DatasetV3(args, args.test_excel_path, 'test')

    testloader = DataLoader(testset, 1, shuffle=False, num_workers=args.num_workers, drop_last=False, pin_memory=False)
    test_loss_bag, avg_score, aucs, thresholds_optimal,f1,precision,recall = test(testloader, milnet, args)
    write_dict_to_csv(args.csv_path,{"acc":avg_score,"auc":aucs,"precision":precision,"recall":recall,"f1":f1},"w" if seed==0 else "a")


def test(testloader, milnet, args):


    milnet.eval()
    total_loss = 0
    test_labels = []
    test_predictions = []
    
    with torch.no_grad():
        for batch_id, (feats,label) in enumerate(testloader):
            bag_feats = feats.cuda()
            bag_prediction, A, H = milnet(bag_feats)

            test_labels.extend([label.squeeze().cpu().numpy()])
            test_predictions.extend([torch.sigmoid(bag_prediction).squeeze().cpu().numpy()])

    test_labels = np.array(test_labels)
    test_predictions = np.array(test_predictions)
    # print(test_labels)
    auc_value, _, thresholds_optimal = multi_label_roc(test_labels, test_predictions, args.num_classes, pos_label=1)
    if args.num_classes==1:
        class_prediction_bag = copy.deepcopy(test_predictions)
        class_prediction_bag[test_predictions>=thresholds_optimal[0]] = 1
        class_prediction_bag[test_predictions<thresholds_optimal[0]] = 0
        test_predictions = class_prediction_bag
        test_labels = np.squeeze(test_labels)
    else:        
        for i in range(args.num_classes):
            class_prediction_bag = copy.deepcopy(test_predictions[:, i])
            class_prediction_bag[test_predictions[:, i]>=thresholds_optimal[i]] = 1
            class_prediction_bag[test_predictions[:, i]<thresholds_optimal[i]] = 0
            test_predictions[:, i] = class_prediction_bag
    bag_score = 0
    for i in range(0, len(testloader)):
        bag_score = np.array_equal(test_labels[i], test_predictions[i]) + bag_score         
    avg_score = bag_score / len(testloader)
    f1 = f1_score(test_labels,test_predictions,average='macro')
    recall = recall_score(test_labels,test_predictions,average='macro')
    precision = precision_score(test_labels,test_predictions,average='macro')
    print(precision,recall,f1)
    return total_loss / len(testloader), avg_score, np.mean(auc_value), thresholds_optimal,f1,precision,recall

def multi_label_roc(labels, predictions, num_classes, pos_label=1):

    thresholds = []
    thresholds_optimal = []
    aucs = []
    if len(predictions.shape)==1: 
        predictions = predictions[:, None]
    for c in range(0, num_classes):
        label = labels[:, c]
        prediction = predictions[:, c]
        fpr, tpr, threshold = roc_curve(label, prediction, pos_label=1)
        fpr_optimal, tpr_optimal, threshold_optimal = optimal_thresh(fpr, tpr, threshold)
        c_auc = roc_auc_score(label, prediction)
        aucs.append(c_auc)
        thresholds.append(threshold)
        thresholds_optimal.append(threshold_optimal)
    return aucs, thresholds, thresholds_optimal

def optimal_thresh(fpr, tpr, thresholds, p=0):
    loss = (fpr - tpr) - p * tpr / (fpr + tpr + 1)
    idx = np.argmin(loss, axis=0)
    return fpr[idx], tpr[idx], thresholds[idx]

def main(dataname,seed):
    args=get_config(dataname)
    args.dataname = dataname
    args.seed = seed
    set_data(args)
    set_seed(seed)

    # <------------- set up logging ------------->
    logging_path = os.path.join(args.save_dir, 'Train_log.log')
    logger = get_logger(logging_path)

    # <------------- save hyperparams ------------->
    option = vars(args)
    file_name = os.path.join(args.save_dir, 'option.txt')
    with open(file_name, 'wt') as opt_file:
        opt_file.write('------------ Options -------------\n')
        for k, v in sorted(option.items()):
            opt_file.write('%s: %s\n' % (str(k), str(v)))
        opt_file.write('-------------- End ----------------\n')


    criterion = nn.BCEWithLogitsLoss()
    
    # <------------- define MIL network ------------->
    milnet = DGRMIL(args.feats_size,L=args.L,n_lesion=args.num_les,dropout_node=args.dropout_node,dropout_patch = args.dropout_patch,num_classes=args.num_classes).cuda()

    optimizer = torch.optim.AdamW(milnet.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    
    scheduler = LinearWarmupCosineAnnealingLR(optimizer,warmup_epochs=args.epoch_des,max_epochs=args.num_epochs,warmup_start_lr=0,eta_min=1e-5)
    
    trainset = C16DatasetV3(args, args.train_excel_path,"train")
    valset = C16DatasetV3(args, args.val_excel_path,'val')

    scaler = GradScaler()
    trainloader = DataLoader(trainset, 1, shuffle=True, num_workers=args.num_workers, drop_last=False, pin_memory=False)
    valloader = DataLoader(valset, 1, shuffle=False, num_workers=args.num_workers, drop_last=False, pin_memory=False)

    best_score = 0
 
    for epoch in range(1, args.num_epochs + 1):
 
        start = False
        if best_score > 0.8:
            start = True
        else:
            start = False
        train_loss_bag = train(trainloader, milnet, criterion, optimizer, epoch,start,args,scaler) # iterate all bags
        
        test_loss_bag, avg_score, aucs, thresholds_optimal,f1,precision,recall = test(valloader, milnet, args)
        
        logger.info('\r Epoch [%d/%d] train loss: %.4f test loss: %.4f, average score: %.4f, f1 score: %.4f, AUC: ' % 
                  (epoch, args.num_epochs, train_loss_bag, test_loss_bag, avg_score,f1) + '|'.join('class-{}>>{}'.format(*k) for k in enumerate(aucs))) 
        
        scheduler.step()
        current_score = (sum(aucs) + avg_score)/3
        if current_score >= best_score:
            best_score = current_score
            print(current_score)
            torch.save(milnet.state_dict(), args.ckp )


if __name__ == '__main__':
    for i in range(5):

        main(dataname="tcga", seed=i)
        test_main(dataname="tcga", seed=i)

        main(dataname="cptac", seed=i)
        test_main(dataname="cptac", seed=i)

        main(dataname="xiangya3", seed=i)
        test_main(dataname="xiangya3", seed=i)


