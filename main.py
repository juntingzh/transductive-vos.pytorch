import argparse
import os
os.environ["NCCL_BLOCKING_WAIT"] = "1"
import time

import torch
import torch.nn as nn
import torch.distributed as dist
import torch.utils.data.distributed
from torch.nn.parallel import DistributedDataParallel
import torch.backends.cudnn as cudnn
import numpy as np
from visdom import Visdom

import dataset
import modeling

from lib.loss import CrossEntropy
from lib.utils import AverageMeter, rgb2class, setup_logger

SCALE = 0.125

def parse_options():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frame_num', '-n', type=int, default=10,
                        help='number of frames to train')
    parser.add_argument('--dataset', '-ds', type=str, default='davis',
                        help='name of dataset')
    parser.add_argument('--data', type=str,
                        help='path to dataset')
    parser.add_argument('--resume', '-r', type=str,
                        help='path to the resumed checkpoint')
    parser.add_argument('--save_model', '-m', type=str, default='./checkpoints',
                        help='directory to save checkpoints')
    parser.add_argument('--epochs', type=int, default=240,
                        help='number of epochs')
    parser.add_argument('--model', type=str, default='resnet50',
                        help='network architecture, resnet18, resnet50 or resnet101')
    parser.add_argument('--temperature', '-t', type=float, default=1.0,
                        help='temperature parameter')
    parser.add_argument('--bs', type=int, default=16,
                        help='batch size')
    parser.add_argument('--lr', type=float, default=0.02,
                        help='initial learning rate')
    parser.add_argument('--wd', type=float, default=3e-4,
                        help='weight decay')  # weight decay
    parser.add_argument('--iter_size', type=int, default=1,
                        help='iter size')
    parser.add_argument('--cj', action='store_true',
                        help='use color jitter')
    parser.add_argument('--local_rank', type=int, default=0,
                        help='default rank for dist')
    parser.add_argument('--val_freq', type=int, default=1,
                        help='validate freq')
    parser.add_argument('--save_freq', type=int, default=1,
                        help='freq to save model')
    parser.add_argument('--log_freq', type=int, default=25,
                        help='freq for logging')

    args = parser.parse_args()

    return args
    
def main(args):

    model = modeling.VOSNet(model=args.model).cuda()
    model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    model = DistributedDataParallel(model, device_ids=[args.local_rank], broadcast_buffers=False)

    criterion = CrossEntropy(temperature=args.temperature).cuda()

    optimizer = torch.optim.SGD(model.parameters(),
                                lr=args.lr,
                                momentum=0.9,
                                nesterov=True,
                                weight_decay=args.wd)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer,
                                                           args.epochs,
                                                           eta_min=4e-5)
    if args.dataset == 'davis':
        train_dataset = dataset.DavisTrain(os.path.join(args.data, 'DAVIS_train/JPEGImages/480p'),
                                           os.path.join(args.data, 'DAVIS_train/Annotations/480p'),
                                           frame_num=args.frame_num,
                                           color_jitter=args.cj)
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        train_loader = torch.utils.data.DataLoader(train_dataset,
                                                   batch_size=args.bs // dist.get_world_size(),
                                                   shuffle=False,
                                                   sampler = train_sampler, 
                                                   pin_memory = True,
                                                   num_workers=8 // dist.get_world_size(),
                                                   drop_last=True)
        val_dataset = dataset.DavisTrain(os.path.join(args.data, 'DAVIS_val/JPEGImages/480p'),
                                         os.path.join(args.data, 'DAVIS_val/Annotations/480p'),
                                         frame_num=args.frame_num,
                                         color_jitter=args.cj)
        val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset)
        val_loader = torch.utils.data.DataLoader(val_dataset,
                                                 batch_size=args.bs // dist.get_world_size(),
                                                 shuffle=False,
                                                 sampler = val_sampler, 
                                                 pin_memory = True,
                                                 num_workers=8 // dist.get_world_size(),
                                                 drop_last=True)
    else:
        raise NotImplementedError
    start_epoch = 0
    if args.resume:
        if os.path.isfile(args.resume):
            logger.info("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume, map_location={'cuda:%d' % 0: 'cuda:%d' % dist.get_rank()})
            start_epoch = checkpoint['epoch']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            scheduler.load_state_dict(checkpoint['scheduler'])
            logger.info("=> loaded checkpoint '{}' (epoch {})"
                        .format(args.resume, checkpoint['epoch']))
        else:
            logger.info("=> no checkpoint found at '{}'".format(args.resume))

    if dist.get_rank() == 0:
        vis = Visdom()
        vis.line(Y=[[0., 0.]],
                 X=[0],
                 opts=dict(title='loss', ylabel='val', xlabel='iter', legend=['loss.val', 'loss.avg']),
                 win='loss')
        vis.line(Y=[args.lr],
                 X=[0],
                 opts=dict(title='lr', ylabel='val', xlabel='iter', legend=['lr']),
                 win='lr',
                 name='lr')
        vis.line(Y=[[0.]],
                 X=[0],
                 opts=dict(title='mean_val_loss', ylabel='val', xlabel='epoch'),
                 win='val_loss',
                 name='val_loss')
    else:
        vis = None

    for epoch in range(start_epoch, args.epochs):

        train_loss = train(train_loader, model, criterion, optimizer, epoch, args, vis=vis)

        if epoch % args.val_freq == 0:
            with torch.no_grad():
                val_loss = validate(val_loader, model, criterion, epoch, args, vis=vis)

        scheduler.step()

        if epoch % args.save_freq == 0 and dist.get_rank() == 0:
            os.makedirs(args.save_model, exist_ok=True)
            checkpoint_name = 'checkpoint-epoch-{}.pth.tar'.format(epoch)
            save_path = os.path.join(args.save_model, checkpoint_name)
            torch.save({
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'scheduler': scheduler.state_dict(),
            }, save_path)
            print('Saved model at {}'.format(save_path))

    os.makedirs(args.save_model, exist_ok=True)
    checkpoint_name = 'checkpoint-final.pth.tar'
    save_path = os.path.join(args.save_model, checkpoint_name)
    torch.save({
        'epoch': args.epochs + 1,
        'state_dict': model.state_dict(),
        'optimizer': optimizer.state_dict(),
        'scheduler': scheduler.state_dict(),
    }, save_path)
    print('Saved model at {}'.format(save_path))


def train(train_loader, model, criterion, optimizer, epoch, args, vis=None):
    logger.info('Starting training epoch {}'.format(epoch))

    centroids = np.load("./dataset/annotation_centroids.npy")
    centroids = torch.Tensor(centroids).float().cuda()

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()

    model.train()

    end = time.time()
    for i, (img_input, annotation_input, _) in enumerate(train_loader):
        data_time.update(time.time() - end)

        (batch_size, num_frames, num_channels, H, W) = img_input.shape
        reshaped_annotation_input = annotation_input.reshape(-1, 3, H, W).cuda()
        annotation_input_downsample = torch.nn.functional.interpolate(reshaped_annotation_input,
                                                                      scale_factor=SCALE,
                                                                      mode='bilinear',
                                                                      align_corners=False)
        H_d = annotation_input_downsample.shape[-2]
        W_d = annotation_input_downsample.shape[-1]

        annotation_input = rgb2class(annotation_input_downsample, centroids)
        annotation_input = annotation_input.reshape(batch_size, num_frames, H_d, W_d)

        img_input = img_input.reshape(-1, num_channels, H, W).cuda()

        features = model(img_input)
        feature_dim = features.shape[1]
        features = features.reshape(batch_size, num_frames, feature_dim, H_d, W_d)

        ref = features[:, 0:num_frames - 1, :, :, :]
        target = features[:, -1, :, :, :]
        ref_label = annotation_input[:, 0:num_frames - 1, :, :]
        target_label = annotation_input[:, -1, :, :]

        ref_label = torch.zeros(batch_size, num_frames - 1, centroids.shape[0], H_d, W_d).cuda().scatter_(
            2, ref_label.unsqueeze(2), 1)

        loss, prediction = criterion(ref, target, ref_label, target_label)
        loss /= args.iter_size
        loss.backward()

        losses.update(loss.item(), batch_size)

        if (i + 1) % args.iter_size == 0:
            optimizer.step()
            optimizer.zero_grad()

        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.log_freq == 0:
            logger.info('Epoch: [{0}][{1}/{2}]\t'
                        'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                        'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                        'Loss {loss.val:.4f} ({loss.avg:.4f})\t'.format(
                epoch, i, len(train_loader), batch_time=batch_time,
                data_time=data_time, loss=losses))
            if vis is not None:
                global_step = epoch * len(train_loader) + i
                vis.line(Y=[[losses.val, losses.avg]],
                         X=[global_step],
                         win='loss',
                         update='append')
                vis.line(Y=[optimizer.param_groups[0]['lr']],
                         X=[global_step],
                         win='lr',
                         update='append')

                images = dataset.davis.denormalize_images(img_input)
                vis.images(images[num_frames - 1::num_frames], opts=dict(caption='input_images'), win='inputs')
                vis.images(reshaped_annotation_input[num_frames - 1::num_frames], opts=dict(caption='annotations'),
                           win='GT')
                upsampled_pred = torch.nn.functional.interpolate(prediction.view(batch_size, -1, H_d, W_d),
                                                                 size=(H, W),
                                                                 mode='bilinear',
                                                                 align_corners=False)
                pred = torch.argmax(upsampled_pred, 1, keepdim=True) * 30  # (B, 1, H, W)
                vis.images(pred, opts=dict(caption='predictions'), win='pred')

    logger.info('Finished training epoch {}'.format(epoch))
    return losses.avg


def validate(val_loader, model, criterion, epoch, args, vis=None):
    logger.info('starting validation...')

    centroids = np.load("./dataset/annotation_centroids.npy")
    centroids = torch.Tensor(centroids).float().cuda()

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()

    model.eval()

    end = time.time()
    for i, (img_input, annotation_input, _) in enumerate(val_loader):

        data_time.update(time.time() - end)

        (batch_size, num_frames, num_channels, H, W) = img_input.shape

        annotation_input = annotation_input.reshape(-1, 3, H, W).cuda()
        annotation_input_downsample = torch.nn.functional.interpolate(annotation_input,
                                                                      scale_factor=SCALE,
                                                                      mode='bilinear',
                                                                      align_corners=False)
        H_d = annotation_input_downsample.shape[-2]
        W_d = annotation_input_downsample.shape[-1]

        annotation_input = rgb2class(annotation_input_downsample, centroids)
        annotation_input = annotation_input.reshape(batch_size, num_frames, H_d, W_d)

        img_input = img_input.reshape(-1, num_channels, H, W).cuda()

        features = model(img_input)
        feature_dim = features.shape[1]
        features = features.reshape(batch_size, num_frames, feature_dim, H_d, W_d)

        ref = features[:, 0:num_frames - 1, :, :, :]
        target = features[:, -1, :, :, :]
        ref_label = annotation_input[:, 0:num_frames - 1, :, :]
        target_label = annotation_input[:, -1, :, :]

        ref_label = torch.zeros(batch_size, num_frames - 1, centroids.shape[0], H_d, W_d).cuda().scatter_(
            2, ref_label.unsqueeze(2), 1)

        loss = criterion(ref, target, ref_label, target_label) / args.iter_size

        losses.update(loss.item(), batch_size)

        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.log_freq == 0:
            logger.info('Validate: [{0}/{1}]\t'
                        'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                        'Loss {loss.val:.4f} ({loss.avg:.4f})\t'.format(
                i, len(val_loader), batch_time=batch_time, loss=losses))
    if vis is not None:
        vis.line(Y=[losses.avg],
                 X=[epoch],
                 win='val_loss',
                 update='append')

    logger.info('Finished validation')
    return losses.avg


if __name__ == '__main__':

    opt = parse_options()

    torch.cuda.set_device(opt.local_rank)
    torch.distributed.init_process_group(backend='nccl', init_method='env://')
    cudnn.benchmark = True

    logger = setup_logger(output=opt.save_model, distributed_rank=dist.get_rank(), name='vos')

    main(opt)
