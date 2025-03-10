import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
from utils.losses.lovasz_losses import lovasz_softmax
from torch.nn.modules.loss import _Loss, _WeightedLoss
from torch.nn import NLLLoss2d


__all__ = ["CrossEntropyLoss2d", "CrossEntropyLoss2dLabelSmooth",
           "FocalLoss2d", "LDAMLoss", "ProbOhemCrossEntropy2d",
           "LovaszSoftmax"]


class CrossEntropyLoss2d(_WeightedLoss):
    """
    Standard pytorch weighted nn.CrossEntropyLoss
    """

    def __init__(self, weight=None, ignore_label=255, reduction='mean'):
        super(CrossEntropyLoss2d, self).__init__()

        self.nll_loss = nn.CrossEntropyLoss(weight, ignore_index=ignore_label, reduction=reduction)

    def forward(self, output, target):
        """
        Forward pass
        :param output: torch.tensor (NxC)
        :param target: torch.tensor (N)
        :return: scalar
        """
        return [self.nll_loss(output, target)]


# class CrossEntropyLoss2d(nn.Module):
#     '''
#     This file defines a cross entropy loss for 2D images
#     '''
#
#     def __init__(self, weight=None, ignore_label=255):
#         '''
#         :param weight: 1D weight vector to deal with the class-imbalance
#         Obtaining log-probabilities in a neural network is easily achieved by adding a LogSoftmax layer in the last layer of your network.
#         You may use CrossEntropyLoss instead, if you prefer not to add an extra layer.
#         '''
#         super().__init__()
#
#         # self.loss = nn.NLLLoss2d(weight, ignore_index=255)
#         self.loss = nn.NLLLoss(weight, ignore_index=ignore_label)
#
#     def forward(self, outputs, targets):
#         return self.loss(F.log_softmax(outputs, dim=1), targets)



class CrossEntropyLoss2dLabelSmooth(_WeightedLoss):
    """
    Refer from https://arxiv.org/pdf/1512.00567.pdf
    :param target: N,
    :param n_classes: int
    :param eta: float
    :return:
        N x C onehot smoothed vector
    """

    def __init__(self, weight=None, ignore_label=255, epsilon=0.1, reduction='mean'):
        super(CrossEntropyLoss2dLabelSmooth, self).__init__()
        self.epsilon = epsilon
        self.nll_loss = nn.CrossEntropyLoss(weight, ignore_index=ignore_label, reduction=reduction)

    def forward(self, output, target):
        """
        Forward pass
        :param output: torch.tensor (NxC)
        :param target: torch.tensor (N)
        :return: scalar
        """
        n_classes = output.size(1)
        # batchsize, num_class = input.size()
        # log_probs = F.log_softmax(inputs, dim=1)
        targets = torch.zeros_like(output).scatter_(1, target.unsqueeze(1), 1)
        targets = (1 - self.epsilon) * targets + self.epsilon / n_classes

        return self.nll_loss(output, targets)


"""
https://arxiv.org/abs/1708.02002
# Credit to https://github.com/clcarwin/focal_loss_pytorch
"""
class FocalLoss2d(nn.Module):
    def __init__(self, alpha=0.5, gamma=2, weight=None, ignore_index=255, reduction='none',
                 size_average=True, balance_weights=[1.0, 0.4]):
        super().__init__()
        self.alpha = alpha
        self.gamma = gamma
        self.weight = weight
        self.ignore_index = ignore_index
        self.size_average = size_average
        self.ce_fn = nn.CrossEntropyLoss(weight=self.weight, ignore_index=self.ignore_index)
        self.criterion = nn.CrossEntropyLoss(reduction=reduction, weight=self.weight, ignore_index=self.ignore_index)
        self.balance_weights = balance_weights
        
    def forward(self, score, target):
        if not (isinstance(score, list) or isinstance(score, tuple)):
            score = [score]
        if len(score) == 2:
            output = score[0]
            early_loss = self.criterion(score[1], target)
            early_loss = early_loss.mean()
            

        if output.dim()>2:
            output = output.contiguous().view(output.size(0), output.size(1), -1)
            output = output.transpose(1,2)
            output = output.contiguous().view(-1, output.size(2)).squeeze()
        if target.dim()==4:
            target = target.contiguous().view(target.size(0), target.size(1), -1)
            target = target.transpose(1,2)
            target = target.contiguous().view(-1, target.size(2)).squeeze()
        elif target.dim()==3:
            target = target.view(-1)
        else:
            target = target.view(-1, 1)

        logpt = self.ce_fn(output, target)
        pt = torch.exp(-logpt)
        local_loss = ((1-pt) ** self.gamma) * self.alpha * logpt
        
        if self.size_average:
            local_loss = local_loss.mean()
        else:
            local_loss = local_loss.sum()
        
        if len(score) == 2:
            loss = self.balance_weights[0] * local_loss + self.balance_weights[1] * early_loss
        else:
            loss = local_loss
            
        return [loss, local_loss, loss - local_loss]


"""
https://arxiv.org/pdf/1906.07413.pdf
"""
class LDAMLoss(nn.Module):

    def __init__(self, cls_num_list, max_m=0.5, weight=None, s=30):
        super(LDAMLoss, self).__init__()
        m_list = 1.0 / np.sqrt(np.sqrt(cls_num_list))
        m_list = m_list * (max_m / np.max(m_list))
        m_list = torch.cuda.FloatTensor(m_list)
        self.m_list = m_list
        assert s > 0
        self.s = s
        self.weight = weight

    def forward(self, x, target):
        index = torch.zeros_like(x, dtype=torch.uint8)
        index.scatter_(1, target.data.view(-1, 1), 1)

        index_float = index.type(torch.cuda.FloatTensor)
        batch_m = torch.matmul(self.m_list[None, :], index_float.transpose(0, 1))
        batch_m = batch_m.view((-1, 1))
        x_m = x - batch_m

        output = torch.where(index, x_m, x)
        return F.cross_entropy(self.s * output, target, weight=self.weight)




# Adapted from OCNet Repository (https://github.com/PkuRainBow/OCNet)
class ProbOhemCrossEntropy2d(nn.Module):
    def __init__(self, ignore_label=255, reduction='none', thresh=0.6, min_kept=256,
                 down_ratio=1, use_weight=False, balance_weights=[1.0, 0.4]):
        super(ProbOhemCrossEntropy2d, self).__init__()
        self.ignore_label = ignore_label
        self.thresh = float(thresh)
        self.min_kept = int(min_kept)
        self.down_ratio = down_ratio
        self.balance_weights = balance_weights
        if use_weight:
            # cityscapes datasets's weight
            print("w/ class balance")
            weight = torch.FloatTensor(
                [0.8373, 0.918, 0.866, 1.0345, 1.0166, 0.9969, 0.9754, 1.0489,
                 0.8786, 1.0023, 0.9539, 0.9843, 1.1116, 0.9037, 1.0865, 1.0955,
                 1.0865, 1.1529, 1.0507])
            self.criterion = nn.CrossEntropyLoss(reduction=reduction,
                                                       weight=weight,
                                                       ignore_index=ignore_label)
        else:
            print("w/o class balance")
            self.criterion = nn.CrossEntropyLoss(reduction=reduction,
                                                       ignore_index=ignore_label)

    def forward(self, score, target):
        if not (isinstance(score, list) or isinstance(score, tuple)):
            score = [score]
        if len(score) == 2:
            early_loss = self.criterion(score[1], target)
            early_loss = early_loss.mean()
        
        # b, c, h, w = pred[0].size()
        # target = target.view(-1)
        # valid_mask = target.ne(self.ignore_label)
        # target = target * valid_mask.long()
        # num_valid = valid_mask.sum()

        # prob = F.softmax(pred[0], dim=1)
        # prob = (prob.transpose(0, 1)).reshape(c, -1)

        # if self.min_kept > num_valid:
        #     print('Labels: {}'.format(num_valid))
        #     pass
        # elif num_valid > 0:
        #     prob = prob.masked_fill_(1 - valid_mask, 1)     #
        #     mask_prob = prob[
        #         target, torch.arange(len(target), dtype=torch.long)]
        #     threshold = self.thresh
        #     if self.min_kept > 0:
        #         index = mask_prob.argsort()
        #         threshold_index = index[min(len(index), self.min_kept) - 1]
        #         if mask_prob[threshold_index] > self.thresh:
        #             threshold = mask_prob[threshold_index]
        #         kept_mask = mask_prob.le(threshold)
        #         target = target * kept_mask.long()
        #         valid_mask = valid_mask * kept_mask
        #         print('Valid Mask: {}'.format(valid_mask.sum()))

        # target = target.masked_fill_(1 - valid_mask, self.ignore_label)
        # target = target.view(b, h, w)

        # ohem_loss = self.criterion(pred[0], target)
        
        
        pred = F.softmax(score[0], dim=1) 
        # [b,c,h,w]->b*c*h*w (展平为一位向量)
        pixel_losses = self.criterion(score[0], target).contiguous().view(-1)
        # mask:b*c*h*w，掩码
        mask = target.contiguous().view(-1) != self.ignore_label 

        # tmp_target:[b,h,w]
        tmp_target = target.clone()
        # 将label值为无效值255的label赋值为0
        tmp_target[tmp_target == self.ignore_label] = 0
        # 使用 gather() 方法根据 tmp_target 张量的值，在 pred 张量的第一维度上
        # 进行索引，获取对应位置的预测概率。这将得到一个形状为 [6, 1, 1024, 1024]
        # 的张量，并将其重新赋值给 pred
        # tmp_target.unsqueeze:[6,1,1024,1024]
        # pred:[6,19,1024,1024]->[6,1,1024,1024]
        pred = pred.gather(1, tmp_target.unsqueeze(1)) 
        # 将 pred 张量展平为一维张量，并根据 mask 掩码选取有效位置的预测概率。
        # 然后，使用 .contiguous().sort()对选取的预测概率进行排序，并返回排序后的
        # 结果 pred 和对应的索引 ind
        # pred：一维张量
        pred, ind = pred.contiguous().view(-1,)[mask].contiguous().sort()
        # config: thres=OHEMTHRES->0.9, min_kept=OHEMKEEP->131072
        min_value = pred[min(self.min_kept, pred.numel() - 1)]
        threshold = max(min_value, self.thresh)

        # 使用mask掩码和ind索引，选取有效位置的像素损失
        # pixel_losses：一维张量
        pixel_losses = pixel_losses[mask][ind]
        # 根据阈值threshold进一步筛选像素损失，只保留对应位置的预测概率小于阈值的损失
        pixel_losses = pixel_losses[pred < threshold]
        ohem_loss = pixel_losses.mean()
        
        if len(score) == 2:
            loss = self.balance_weights[0] * ohem_loss + self.balance_weights[1] * early_loss
        else:
            loss = ohem_loss
            
        return [loss, ohem_loss, loss - ohem_loss]
        


# ==========================================================================================================================
# ==========================================================================================================================
# class-balanced loss
class CrossEntropy2d(nn.Module):

    def __init__(self, size_average=True, ignore_label=255, use_weight=True):
        super(CrossEntropy2d, self).__init__()
        self.size_average = size_average
        self.ignore_label = ignore_label
        self.use_weight   = use_weight
        # if self.use_weight:
        #     self.weight = torch.FloatTensor(
        #         [0.8373, 0.918, 0.866, 1.0345, 1.0166, 0.9969, 0.9754, 1.0489,
        #          0.8786, 1.0023, 0.9539, 0.9843, 1.1116, 0.9037, 1.0865, 1.0955,
        #          1.0865, 1.1529, 1.0507])
        #     print('CrossEntropy2d weights : {}'.format(self.weight))
        # else:
        #     self.weight = None


    def forward(self, predict, target, weight=None):

        """
            Args:
                predict:(n, c, h, w)
                target:(n, h, w)
                weight (Tensor, optional): a manual rescaling weight given to each class.
                                           If given, has to be a Tensor of size "nclasses"
        """
        # Variable(torch.randn(2,10)
        if self.use_weight:
            print('target size {}'.format(target.shape))
            freq = np.zeros(19)
            for k in range(19):
                mask = (target[:, :, :] == k)
                freq[k] = torch.sum(mask)
                print('{}th frequency {}'.format(k, freq[k]))
            weight = freq / np.sum(freq)
            print(weight)
            self.weight = torch.FloatTensor(weight)
            print('Online class weight: {}'.format(self.weight))
        else:
            self.weight = None


        criterion = nn.CrossEntropyLoss(weight=self.weight, ignore_index=self.ignore_label)
        # torch.FloatTensor([2.87, 13.19, 5.11, 37.98, 35.14, 30.9, 26.23, 40.24, 6.66, 32.07, 21.08, 28.14, 46.01, 10.35, 44.25, 44.9, 44.25, 47.87, 40.39])
        #weight = Variable(torch.FloatTensor([1, 1.49, 1.28, 1.62, 1.62, 1.62, 1.64, 1.62, 1.49, 1.62, 1.43, 1.62, 1.64, 1.43, 1.64, 1.64, 1.64, 1.64, 1.62]), requires_grad=False).cuda()
        assert not target.requires_grad
        assert predict.dim() == 4
        assert target.dim() == 3
        assert predict.size(0) == target.size(0), "{0} vs {1} ".format(predict.size(0), target.size(0))
        assert predict.size(2) == target.size(1), "{0} vs {1} ".format(predict.size(2), target.size(1))
        assert predict.size(3) == target.size(2), "{0} vs {1} ".format(predict.size(3), target.size(3))
        n, c, h, w = predict.size()
        target_mask = (target >= 0) * (target != self.ignore_label)
        target = target[target_mask]
        if not target.data.dim():
            return torch.zeros(1)
        predict = predict.transpose(1, 2).transpose(2, 3).contiguous()
        predict = predict[target_mask.view(n, h, w, 1).repeat(1, 1, 1, c)].view(-1, c)
        loss = criterion(predict, target)
        return loss
# ==========================================================================================================================
# ==========================================================================================================================




class LovaszSoftmax(nn.Module):
    def __init__(self, classes='present', per_image=False, ignore_index=255):
        super(LovaszSoftmax, self).__init__()
        self.smooth = classes
        self.per_image = per_image
        self.ignore_index = ignore_index

    def forward(self, output, target):
        logits = F.softmax(output, dim=1)
        loss = lovasz_softmax(logits, target, ignore=self.ignore_index)
        return loss
