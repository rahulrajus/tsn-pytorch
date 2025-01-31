import argparse
import time

import numpy as np
import torch.nn.parallel
import torch.optim
import torch
from sklearn.metrics import confusion_matrix

from dataset import TSNDataSet
from models import TSN
from transforms import *
from ops import ConsensusModule

# options
parser = argparse.ArgumentParser(
    description="Standard video-level testing")
parser.add_argument('dataset', type=str, choices=[
                    'ucf101', 'hmdb51', 'kinetics'])
parser.add_argument('modality', type=str, choices=['RGB', 'Flow', 'RGBDiff'])
parser.add_argument('test_list', type=str)
parser.add_argument('weights', type=str)
parser.add_argument('--arch', type=str, default="resnet101")
parser.add_argument('--save_scores', type=str, default=None)
parser.add_argument('--test_segments', type=int, default=1)
parser.add_argument('--max_num', type=int, default=-1)
parser.add_argument('--test_crops', type=int, default=1)
parser.add_argument('--input_size', type=int, default=224)
parser.add_argument('--crop_fusion_type', type=str, default='avg',
                    choices=['avg', 'max', 'topk'])
parser.add_argument('--k', type=int, default=3)
parser.add_argument('--dropout', type=float, default=0.7)
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--gpus', nargs='+', type=int, default=None)
parser.add_argument('--flow_prefix', type=str, default='')

args = parser.parse_args()


if args.dataset == 'ucf101':
    num_class = 101
elif args.dataset == 'hmdb51':
    num_class = 51
elif args.dataset == 'kinetics':
    num_class = 400
# else:
#     raise ValueError('Unknown dataset '+args.dataset)
activation = {}


def get_activation(name):
    def hook(model, input, output):
        activation[name] = output.detach()
    return hook


net = TSN(num_class, 1, args.modality,
          base_model=args.arch,
          consensus_type=args.crop_fusion_type,
          dropout=args.dropout)
print(net)
net.base_model.fc.register_forward_hook(get_activation('fc'))
ctx = {
    'modality': net.modality,
    'new_length': net.new_length,
    '_get_diff': net._get_diff,
    'base_model': net.base_model,
    'dropout': net.dropout,
    'new_fc': net.new_fc,
    'before_softmax': net.before_softmax,
    'reshape': net.reshape,
    'num_segments': net.num_segments,
    'consensus': net.consensus
}
checkpoint = torch.load(args.weights)
# print(
# "model epoch {} best prec@1: {}".format(checkpoint['epoch'], checkpoint['best_prec1']))
# print(checkpoint)
# print(net)
# base_dict = {'.'.join(k.split('.')[1:]): v for k, v in list(
#     checkpoint.items())}
base_dict = {}
count = 0
for k, v in checkpoint.items():
    count = count + 1
    if 415 > count > 18:
        base_dict.setdefault(k[7:], checkpoint[k])
    if count < 19:
        base_dict.setdefault(k, checkpoint[k])
base_dict.setdefault(
    'new_fc.weight', checkpoint['base_model.fc_action.1.weight'])
base_dict.setdefault('new_fc.bias', checkpoint['base_model.fc_action.1.bias'])

net.load_state_dict(base_dict)

if args.test_crops == 1:
    cropping = torchvision.transforms.Compose([
        GroupScale(net.scale_size),
        GroupCenterCrop(net.input_size),
    ])
elif args.test_crops == 10:
    cropping = torchvision.transforms.Compose([
        GroupOverSample(net.input_size, net.scale_size)
    ])
else:
    raise ValueError(
        "Only 1 and 10 crops are supported while we got {}".format(args.test_crops))

data_loader = torch.utils.data.DataLoader(
    TSNDataSet("", args.test_list, num_segments=2999,
               new_length=1 if args.modality == "RGB" else 5,
               modality=args.modality,
               image_tmpl="{:06d}.jpg" if args.modality in [
                   'RGB', 'RGBDiff'] else args.flow_prefix+"{}_{:05d}.jpg",
               test_mode=True,
               transform=torchvision.transforms.Compose([
                   cropping,
                   Stack(roll=args.arch == 'BNInception'),
                   ToTorchFormatTensor(div=args.arch != 'BNInception'),
                   GroupNormalize(net.input_mean, net.input_std),
               ])),
    batch_size=1, shuffle=False,
    num_workers=args.workers * 2, pin_memory=True)

if args.gpus is not None:
    devices = [args.gpus[i] for i in range(args.workers)]
else:
    devices = list(range(args.workers))


net = torch.nn.DataParallel(net.cuda(devices[0]), device_ids=devices)
net.eval()

data_gen = enumerate(data_loader)

total_num = len(data_loader.dataset)
output = []

ft = []


def get_feature_frames(video_data):
    i, data, label = video_data
    print("SHAPE", data.shape)
    num_crop = args.test_crops

    if args.modality == 'RGB':
        length = 3
    elif args.modality == 'Flow':
        length = 10
    else:
        raise ValueError("Unknown modality "+args.modality)
    input_var = torch.autograd.Variable(data.view(-1, length, data.size(2), data.size(3)),
                                        volatile=True)
    input_split = torch.split(input_var, 25)
    print("INPUT", input_var.shape)
    print("INPUT SPLIT", input_split)
    print("INPUT SPLIT", len(input_split))
    print("INPUT SPLIT", input_split[0].shape)
    feats = []
    for frame_batch in input_split:
        rst = net(ctx, frame_batch).data.cpu().numpy().copy()
        feats.append(activation['fc'])
    feat_stack = torch.cat(feats)
    print(feat_stack)
    print(feat_stack.shape)
    # return feats

    # rst = net(ctx, input_var).data.cpu().numpy().copy()
    # print("FEATURE: ", activation['fc'])
    # print(activation['fc'].shape)
    ft.append(activation['fc'])
    return i, rst.reshape((num_crop, args.test_segments, num_class)).mean(axis=0).reshape(
        (args.test_segments, 1, num_class)
    ), label[0]


def eval_video(video_data):
    i, data, label = video_data
    print("SHAPE", data.shape)
    num_crop = args.test_crops

    if args.modality == 'RGB':
        length = 3
    elif args.modality == 'Flow':
        length = 10
    elif args.modality == 'RGBDiff':
        length = 18
    else:
        raise ValueError("Unknown modality "+args.modality)

    input_var = torch.autograd.Variable(data.view(-1, length, data.size(2), data.size(3)),
                                        volatile=True)
    print("INPUT", input_var.shape)
    rst = net(ctx, input_var).data.cpu().numpy().copy()
    print("FEATURE: ", activation['fc'])
    print(activation['fc'].shape)
    ft.append(activation['fc'])
    return i, rst.reshape((num_crop, args.test_segments, num_class)).mean(axis=0).reshape(
        (args.test_segments, 1, num_class)
    ), label[0]


proc_start_time = time.time()
max_num = args.max_num if args.max_num > 0 else len(data_loader.dataset)

for i, (data, label) in data_gen:
    if i >= max_num:
        break
    print("DATA SHAPE", data.shape)
    rst = get_feature_frames((i, data, label))
    output.append(rst[1:])
    cnt_time = time.time() - proc_start_time
    print('video {} done, total {}/{}, average {} sec/video'.format(i, i+1,
                                                                    total_num,
                                                                    float(cnt_time) / (i+1)))
feat_tot = torch.cat(ft)
print(feat_tot.shape)
video_pred = [np.argmax(np.mean(x[0], axis=0)) for x in output]

video_labels = [x[1] for x in output]


cf = confusion_matrix(video_labels, video_pred).astype(float)

cls_cnt = cf.sum(axis=1)
cls_hit = np.diag(cf)

cls_acc = cls_hit / cls_cnt

print(cls_acc)

print('Accuracy {:.02f}%'.format(np.mean(cls_acc) * 100))

if args.save_scores is not None:

    # reorder before saving
    name_list = [x.strip().split()[0] for x in open(args.test_list)]

    order_dict = {e: i for i, e in enumerate(sorted(name_list))}

    reorder_output = [None] * len(output)
    reorder_label = [None] * len(output)

    for i in range(len(output)):
        idx = order_dict[name_list[i]]
        reorder_output[idx] = output[i]
        reorder_label[idx] = video_labels[i]

    np.savez(args.save_scores, scores=reorder_output, labels=reorder_label)
