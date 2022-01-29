import argparse
import functools
import copy
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl
from multimodal.multimodal import MultiModalModel, LanguageModel
from multimodal.utils import get_entropy
from multimodal.multimodal_data_module import \
    PAD_TOKEN_ID, SOS_TOKEN_ID, EOS_TOKEN_ID

OPTIMIZER = torch.optim.AdamW
LR = 3e-4
WEIGHT_DECAY = 0.01
# SELF_DISTILLATION = False
# ALPHA = 1


class MultiModalLitModel(pl.LightningModule):
    """
    PyTorch Lightning class for MultiModal SAYCam model
    """

    def __init__(self, vision_encoder, text_encoder, args):
        super().__init__()
        self.args = vars(args) if args is not None else {}

        self.optimizer_class = self.args.get("optimizer", OPTIMIZER)
        self.lr = self.args.get("lr", LR)
        self.weight_decay = self.args.get("weight_decay", WEIGHT_DECAY)
        # self.alpha = self.args.get("alpha", ALPHA)
        self.lambda_mm = self.args.get("lambda_mm", 1.)
        self.lambda_lm = self.args.get("lambda_lm", 0.)
        self.optimize_unused = self.args.get("optimize_unused", False)

        self.vision_encoder = vision_encoder
        self.text_encoder = text_encoder
        self.model = MultiModalModel(
            self.vision_encoder, self.text_encoder, args)
        self.language_model = LanguageModel(self.text_encoder, args)

        # self-distillation
        # self.self_distillation = self.args.get(
        #     "self_distillation", SELF_DISTILLATION)

        # if self.self_distillation:
        #     # only instantiate teacher model if self-distillation is on
        #     self.teacher = copy.deepcopy(self.model)

        #     # set teacher to be non-trainable
        #     for param in self.teacher.parameters():
        #         param.requires_grad = False

        # save hyperparameters to logger
        self.save_hyperparameters()

    @staticmethod
    def add_to_argparse(parser):
        parser.add_argument("--optimizer", type=lambda o: getattr(torch.optim, o), default=OPTIMIZER,
                            help="optimizer class under toch.optim")
        parser.add_argument("--lr", type=float, default=LR,
                            help="learning rate")
        parser.add_argument("--weight_decay", type=float, default=WEIGHT_DECAY,
                            help="weight decay on all parameters")
        parser.add_argument("--lambda_mm", type=float, default=1.,
                            help="multimodal loss *= lambda_mm")
        parser.add_argument("--lambda_lm", type=float, default=0.,
                            help="lm loss *= lambda_lm")
        parser.add_argument("--optimize_unused", action="store_true",
                            help="optimize the computation for unused loss")
        # parser.add_argument("--self_distillation", action='store_true',
        #                     help="include self-distillation loss during training")
        # parser.add_argument("--alpha", type=float, default=1.0,
        #                     help="coefficient for KLdiv loss in self-distillation")

    def configure_optimizers(self):
        optimizer = self.optimizer_class(
            self.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        return optimizer

    def forward(self, x, y, y_len):
        return self.model(x, y, y_len)

    # def calculate_self_distillation_loss(self, x, y, y_len):
    #     # get teacher targets and student predictions
    #     teacher_logits_per_image, teacher_logits_per_text = self.teacher(
    #         x, y, y_len, self_distillation=True, teacher=True)
    #     student_logits_per_image, student_logits_per_text = self.model(
    #         x, y, y_len, self_distillation=True, teacher=False)

    #     # calculate kl div loss
    #     kl_loss = (F.kl_div(F.log_softmax(student_logits_per_image, dim=-1), teacher_logits_per_image, reduction='batchmean') +
    #                F.kl_div(F.log_softmax(student_logits_per_text, dim=-1), teacher_logits_per_text, reduction='batchmean')).div(2) * self.alpha

    #     # update teacher model via ema
    #     self.update_teacher()

    #     return kl_loss

    def calculate_joint_loss(self, batch, stage, log):
        # batch of image-text pairs
        x, y, y_len = batch

        # dict of results to return
        ret = {
            'batch_size': x.size(0),
        }

        # reuse image_features and text_outputs if possible
        image_features, text_outputs = None, None

        if self.lambda_mm or not self.optimize_unused:
            infonce_loss, image_accuracy, text_accuracy, \
            image_entropy, text_entropy, logits_per_image, logits_per_text, \
            image_features, text_outputs = \
            self.model.calculate_contrastive_loss(x, y, y_len)

            # if self.self_distillation:
            #     kl_loss = self.calculate_self_distillation_loss(x, y, y_len)
            # else:
            #     kl_loss = 0.

            # log
            log(f"{stage}_infonce_loss", infonce_loss)
            # log(f"{stage}_kl_loss", kl_loss)
            log(f"{stage}_image_accuracy", image_accuracy)
            log(f"{stage}_text_accuracy", text_accuracy)
            log(f"{stage}_image_entropy", image_entropy)
            log(f"{stage}_text_entropy", text_entropy)
            log("temperature",
                     (-self.model.logit_neg_log_temperature).exp().item())
            # log("kl_temperature", (-self.model.kl_logit_neg_log_temperature).exp().item())

            ret.update({
                'infonce_loss': infonce_loss.detach(),
                'image_accuracy': image_accuracy,
                'text_accuracy': text_accuracy,
                'image_entropy': image_entropy.detach(),
                'text_entropy': text_entropy.detach(),
            })

        else:
            infonce_loss = 0.

        if self.lambda_lm or not self.optimize_unused:
            if self.language_model.text_encoder.captioning:
                # get image_features if needed
                if image_features is None:
                    image_features = self.vision_encoder(x)
                    if self.model.normalize_features:
                        image_features = F.normalize(image_features, p=2, dim=1)  # normalize image features
                # text_outputs is not reusable since it's not obtained from captioning in the contrastive module
                text_outputs = None

            # calculate language model ce loss
            ce_loss, _, _, labels = self.language_model.calculate_ce_loss(
                y, y_len, outputs=text_outputs, image_features=image_features,
                tokenwise=True)

            # get all kinds of losses with/without special tokens
            # standard loss including all special tokens
            mask = (labels != PAD_TOKEN_ID)
            n_tokens = mask.sum()
            lm_ce_loss = ce_loss.sum() / n_tokens
            # excluding SOS_TOKEN
            mask = mask & (labels != SOS_TOKEN_ID)
            n_tokens_wo_sos = mask.sum()
            lm_ce_loss_wo_sos = (ce_loss * mask).sum() / n_tokens_wo_sos
            # further excluding EOS_TOKEN
            mask = mask & (labels != EOS_TOKEN_ID)
            n_tokens_wo_sos_eos = mask.sum()
            lm_ce_loss_wo_sos_eos = (ce_loss * mask).sum() / n_tokens_wo_sos_eos

            # log
            log(f"{stage}_ce_loss", lm_ce_loss)
            log(f"{stage}_ce_loss_wo_sos", lm_ce_loss_wo_sos)
            log(f"{stage}_ce_loss_wo_sos_eos", lm_ce_loss_wo_sos_eos)

            ret.update({
                'ce_loss': lm_ce_loss.detach(),
                'ce_loss_wo_sos': lm_ce_loss_wo_sos.detach(),
                'ce_loss_wo_sos_eos': lm_ce_loss_wo_sos_eos.detach(),
                'n_tokens': n_tokens,
                'n_tokens_wo_sos': n_tokens_wo_sos,
                'n_tokens_wo_sos_eos': n_tokens_wo_sos_eos,
            })

        else:
            lm_ce_loss = 0.

        # calculate joint loss
        loss = self.lambda_mm * infonce_loss + self.lambda_lm * lm_ce_loss

        # log
        log(f"{stage}_loss", loss)

        ret.update({
            'loss': loss,
        })

        return ret

    def joint_loss_epoch_end(self, outputs, stage, log):
        def mean_over_examples(name):
            # mean over examples
            n_examples = 0
            value_sum = 0.
            for output in outputs:
                batch_size = output['batch_size']
                value = output[name].item()
                n_examples += batch_size
                value_sum += value * batch_size
            value_mean = value_sum / n_examples
            return value_mean

        def mean_over_tokens(name, n_tokens_name):
            # mean over tokens
            n_tokens_sum = 0
            value_sum = 0.
            for output in outputs:
                n_tokens = output[n_tokens_name].item()
                value = output[name].item()
                n_tokens_sum += n_tokens
                value_sum += value * n_tokens
            value_mean = value_sum / n_tokens_sum
            return value_mean

        if self.lambda_mm or not self.optimize_unused:
            for name in (
                'infonce_loss', 'image_accuracy', 'text_accuracy',
                'image_entropy', 'text_entropy',):
                log(f"{stage}_{name}", mean_over_examples(name))

        if self.lambda_lm or not self.optimize_unused:
            for suffix in ('', '_wo_sos', '_wo_sos_eos'):
                value_mean = mean_over_tokens(
                    f'ce_loss{suffix}', f'n_tokens{suffix}')
                log(f"{stage}_ce_loss{suffix}", value_mean)

                # perplexity
                perplexity = np.exp(value_mean)
                log(f"{stage}_perplexity{suffix}", perplexity)

        for name in ('loss',):
            log(f"{stage}_{name}", mean_over_examples(name))

    def training_step(self, batch, batch_idx):
        return self.calculate_joint_loss(batch, 'train', self.log)

    def training_epoch_end(self, outputs):
        log = lambda name, value, *args, **kwargs: self.log(
            f'{name}_epoch', value, on_step=False, on_epoch=True,
            *args, **kwargs)
        return self.joint_loss_epoch_end(outputs, 'train', log)

    def validation_test_step(self, stage, batch, batch_idx, dataloader_idx=0):
        log = functools.partial(self.log, on_step=False, on_epoch=True)

        ret = {}

        if dataloader_idx == 0:
            empty_log = lambda *args, **kwargs: None
            ret.update(self.calculate_joint_loss(batch, stage, empty_log))

        elif dataloader_idx == 1:
            # TODO: check whether adding special tokens will make a difference

            # batch of evaluation trials (only one trial at a time)
            x, y, y_len = batch

            if self.lambda_mm or not self.optimize_unused:
                # resize x so images from the same trial are in the batch dim
                # [B, N, C, H, W] -> [B*N, C, H, W]  (with B = 1)
                x = x.view(-1, *x.shape[-3:])

                # calculate accuracy
                logits_per_image, logits_per_text = self.model(x, y, y_len)
                logits = logits_per_text[0]  # get logits per trial
                pred = torch.argmax(logits).item()
                label = 0  # correct answer is always the first item
                accuracy = int(pred == label)
                entropy = get_entropy(logits)

                # log evaluation accuracy and entropy
                log(f"{stage}_accuracy", accuracy)
                log(f"{stage}_entropy", entropy)

                # log category-level evaluation accuracies as a separate metric
                category_label = self.text_encoder.idx2word[y.item()]
                log(f"{stage}_accuracy_{category_label}", accuracy)

                ret.update({'accuracy': accuracy})

            else:
                accuracy = 0.

        return ret

    def validation_test_epoch_end(self, stage, outputs):
        # only deal with outputs of the first dataset
        log = functools.partial(self.log, on_step=False, on_epoch=True)
        if len(outputs) == 2 and isinstance(outputs[0], list):  # multiple val dataloaders
            outputs = outputs[0]
        return self.joint_loss_epoch_end(outputs, stage, log)

    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        return self.validation_test_step(
            'val', batch, batch_idx, dataloader_idx=dataloader_idx)

    def validation_epoch_end(self, outputs):
        return self.validation_test_epoch_end(
            'val', outputs)

    def test_step(self, batch, batch_idx, dataloader_idx=0):
        return self.validation_test_step(
            'test', batch, batch_idx, dataloader_idx=dataloader_idx)

    def test_epoch_end(self, outputs):
        return self.validation_test_epoch_end(
            'test', outputs)

    # def update_teacher(self):
    #     for teacher, student in zip(self.teacher.parameters(), self.model.parameters()):
    #         teacher.data.copy_(self.ema(teacher.data, student.data))

    # def ema(self, s, t):
    #     return s * (1 - 0.999) + t * 0.999
