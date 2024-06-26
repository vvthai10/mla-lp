import argparse
import random
import numpy as np
import torch
from torch.nn import functional as F
from tqdm import tqdm
from sklearn.metrics import roc_auc_score
from dataset.medical_zero import MedTestDataset, MedTrainDataset
from CLIP.clip import create_model
from CLIP.adapter import CLIP_Inplanted
from loss import FocalLoss, BinaryDiceLoss
from utils import encode_text_with_prompt_ensemble
from prompt import REAL_NAME

import warnings

warnings.filterwarnings("ignore")

use_cuda = torch.cuda.is_available()
device = torch.device("cuda:0" if use_cuda else "cpu")
torch.backends.cudnn.benchmark = True
NUM_WORKERS = 8


CLASS_INDEX = {
    "Brain": 3,
    "Liver": 2,
    "Retina_RESC": 1,
    "Retina_OCT2017": -1,
    "Chest": -2,
    "Histopathology": -3,
}  #
CLASS_INDEX_INV = {
    3: "Brain",
    2: "Liver",
    1: "Retina_RESC",
    -1: "Retina_OCT2017",
    -2: "Chest",
    -3: "Histopathology",
}  #


def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def main():
    parser = argparse.ArgumentParser(description="Testing")
    parser.add_argument(
        "--model_name",
        type=str,
        default="ViT-L-14-336",
        help="ViT-B-16-plus-240, ViT-L-14-336",
    )
    parser.add_argument(
        "--pretrain", type=str, default="openai", help="laion400m, openai"
    )
    parser.add_argument("--obj", type=str, default="Retina_RESC")
    parser.add_argument("--data_path", type=str, default="./data/")
    parser.add_argument("--ckpt_path", type=str, default="./ckpt/")
    parser.add_argument("--batch_size", type=int, default=2)
    parser.add_argument("--img_size", type=int, default=240)
    parser.add_argument("--epoch", type=int, default=50, help="epochs")
    parser.add_argument(
        "--learning_rate", type=float, default=0.0001, help="learning rate"
    )
    parser.add_argument(
        "--features_list",
        type=int,
        nargs="+",
        default=[6, 12, 18, 24],
        help="features used",
    )
    parser.add_argument("--seed", type=int, default=111)
    args = parser.parse_args()

    setup_seed(args.seed)

    # fixed feature extractor
    clip_model = create_model(
        model_name=args.model_name,
        img_size=args.img_size,
        device=device,
        pretrained=args.pretrain,
        require_pretrained=True,
    )

    clip_model.eval()
    clip_model.visual.DAPM_replace(DPAM_layer=20)

    model = CLIP_Inplanted(
        clip_model=clip_model,
        features=args.features_list,
        seg_reduce_dim=128,
        det_reduce_dim=768,
    ).to(device)

    model.eval()

    for name, param in model.named_parameters():
        param.requires_grad = True

    # optimizer for only adapters
    seg_optimizer = torch.optim.Adam(
        list(model.seg_adapters.parameters()), lr=args.learning_rate, betas=(0.5, 0.999)
    )

    det_optimizer = torch.optim.Adam(
        list(model.det_adapters.parameters()), lr=args.learning_rate, betas=(0.5, 0.999)
    )

    decoder_optimizer = torch.optim.Adam(
        list(model.decoder.parameters()), lr=args.learning_rate, betas=(0.5, 0.999)
    )

    text_proj_optimizer = torch.optim.Adam(
        list(model.text_proj.parameters()), lr=args.learning_rate, betas=(0.5, 0.999)
    )

    # load dataset and loader
    kwargs = {"num_workers": NUM_WORKERS, "pin_memory": True} if use_cuda else {}
    train_dataset = MedTrainDataset(
        args.data_path, args.obj, args.img_size, args.batch_size
    )
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=1, shuffle=True, **kwargs
    )

    test_dataset = MedTestDataset(args.data_path, args.obj, args.img_size)
    test_loader = torch.utils.data.DataLoader(
        test_dataset, batch_size=1, shuffle=False, **kwargs
    )

    # losses
    loss_focal = FocalLoss()
    loss_dice = BinaryDiceLoss()
    loss_bce = torch.nn.BCEWithLogitsLoss()

    text_feature_list = [0]

    # text prompt
    with torch.cuda.amp.autocast(), torch.no_grad():
        for i in [1, 2, 3, -3, -2, -1]:  #
            text_feature = encode_text_with_prompt_ensemble(
                clip_model,
                REAL_NAME[CLASS_INDEX_INV[i]],
                device,
            )
            text_feature_list.append(text_feature)

    save_score = 0.0

    total_det = sum(
        p.numel() for p in model.det_adapters.parameters() if p.requires_grad
    )

    total_seg = sum(
        p.numel() for p in model.seg_adapters.parameters() if p.requires_grad
    )

    total_dec = sum(p.numel() for p in model.decoder.parameters() if p.requires_grad)

    total_text_proj = sum(
        p.numel() for p in model.text_proj.parameters() if p.requires_grad
    )

    print("Parameters: ")
    print("Classification params: ", total_det)
    print("Segmentation params: ", total_seg)
    print("Decoder params: ", total_dec)
    print("Text projection params: ", total_text_proj)

    for epoch in range(args.epoch):
        print("epoch", epoch, ":")
        if epoch > 0:
            score = test(
                args, model, test_loader, text_feature_list[CLASS_INDEX[args.obj]]
            )
            if score >= save_score:
                save_score = score
                ckp_path = f"{args.ckpt_path}/zero-shot/{args.obj}.pth"
                torch.save(
                    {
                        "seg_adapters": model.seg_adapters.state_dict(),
                        "det_adapters": model.det_adapters.state_dict(),
                        "decoder": model.decoder.state_dict(),
                        "text_proj": model.text_proj.state_dict(),
                    },
                    ckp_path,
                )
                print(f"best epoch found: epoch {epoch} ")
            print("\n")

        loss_list = []
        for image, image_label, mask, seg_idx in tqdm(train_loader):

            image = image.squeeze(0).to(device)
            seg_idx = seg_idx.item()

            with torch.cuda.amp.autocast():
                _, seg_patch_tokens, det_patch_tokens = model(image)

                # seg_patch_tokens = [p[:, 1:, :] for p in seg_patch_tokens]
                det_patch_tokens = [p[:, 1:, :] for p in det_patch_tokens]

                # image level
                det_loss = 0
                image_label = image_label.squeeze(0).to(device)

                for layer in range(len(det_patch_tokens)):
                    det_patch_tokens[layer] = det_patch_tokens[
                        layer
                    ] / det_patch_tokens[layer].norm(dim=-1, keepdim=True)
                    anomaly_map = (
                        100.0 * det_patch_tokens[layer] @ text_feature_list[seg_idx]
                    )
                    anomaly_map = torch.softmax(anomaly_map, dim=-1)[:, :, 1]
                    anomaly_score = torch.mean(anomaly_map, dim=-1)
                    det_loss += loss_bce(anomaly_score, image_label)

                if seg_idx > 0:
                    # pixel level
                    seg_loss = 0
                    mask = mask.squeeze(0).to(device)
                    mask[mask > 0.5], mask[mask <= 0.5] = 1, 0

                    for layer in range(len(seg_patch_tokens)):

                        if layer == 0:
                            x = seg_patch_tokens[layer]
                        else:
                            x = 0.5 * x + 0.5 * seg_patch_tokens[layer]

                        x, anomaly_map = model.decode(
                            patch_tokens=x,
                            text_features=text_feature_list[seg_idx],
                            ith=layer,
                        )

                        B, L, C = anomaly_map.shape
                        H = int(np.sqrt(L))
                        anomaly_map = F.interpolate(
                            anomaly_map.permute(0, 2, 1).view(B, 2, H, H),
                            size=args.img_size,
                            mode="bilinear",
                            align_corners=True,
                        )
                        anomaly_map = torch.softmax(anomaly_map, dim=1)
                        seg_loss += loss_focal(anomaly_map, mask)
                        seg_loss += loss_dice(anomaly_map[:, 1, :, :], mask)

                    loss = (
                        seg_loss + det_loss
                    )  # = focal(seg_out, mask) + bce(det_out, y)

                    loss.requires_grad_(True)

                    seg_optimizer.zero_grad()
                    det_optimizer.zero_grad()
                    decoder_optimizer.zero_grad()
                    text_proj_optimizer.zero_grad()

                    loss.backward()

                    seg_optimizer.step()
                    det_optimizer.step()
                    decoder_optimizer.step()
                    text_proj_optimizer.step()

                else:
                    loss = det_loss
                    loss.requires_grad_(True)

                    det_optimizer.zero_grad()
                    decoder_optimizer.zero_grad()
                    text_proj_optimizer.zero_grad()

                    loss.backward()

                    det_optimizer.step()
                    decoder_optimizer.step()
                    text_proj_optimizer.step()

                loss_list.append(loss.item())

        train_dataset.shuffle_dataset()
        train_loader = torch.utils.data.DataLoader(
            train_dataset, batch_size=1, shuffle=True, **kwargs
        )

        # logs
        print("Loss: ", np.mean(loss_list))


def test(args, seg_model, test_loader, text_features):
    gt_list = []
    gt_mask_list = []
    image_scores = []
    segment_scores = []

    for image, y, mask in tqdm(test_loader):
        image = image.to(device)
        mask[mask > 0.5], mask[mask <= 0.5] = 1, 0

        with torch.no_grad(), torch.cuda.amp.autocast():
            _, ori_seg_patch_tokens, ori_det_patch_tokens = seg_model(image)
            # ori_seg_patch_tokens = [p[0, 1:, :] for p in ori_seg_patch_tokens]
            ori_det_patch_tokens = [p[0, 1:, :] for p in ori_det_patch_tokens]

            # image
            anomaly_score = 0
            patch_tokens = ori_det_patch_tokens.copy()
            for layer in range(len(patch_tokens)):
                patch_tokens[layer] /= patch_tokens[layer].norm(dim=-1, keepdim=True)
                anomaly_map = (100.0 * patch_tokens[layer] @ text_features).unsqueeze(0)
                anomaly_map = torch.softmax(anomaly_map, dim=-1)[:, :, 1]
                anomaly_score += anomaly_map.mean()

            image_scores.append(anomaly_score.cpu())

            # pixel
            patch_tokens = ori_seg_patch_tokens
            anomaly_maps = []
            for layer in range(len(patch_tokens)):
                if layer == 0:
                    x = patch_tokens[layer]
                else:
                    x = 0.5 * x + 0.5 * patch_tokens[layer]
                # patch_tokens[layer] /= patch_tokens[layer].norm(dim=-1, keepdim=True)
                # anomaly_map = (100.0 * patch_tokens[layer] @ text_features).unsqueeze(0)
                x, anomaly_map = seg_model.decode(
                    patch_tokens=x, text_features=text_features, ith=layer
                )
                B, L, C = anomaly_map.shape
                H = int(np.sqrt(L))
                anomaly_map = F.interpolate(
                    anomaly_map.permute(0, 2, 1).view(B, 2, H, H),
                    size=args.img_size,
                    mode="bilinear",
                    align_corners=True,
                )
                anomaly_map = torch.softmax(anomaly_map, dim=1)[:, 1, :, :]
                anomaly_maps.append(anomaly_map.cpu().numpy())
            final_score_map = np.sum(anomaly_maps, axis=0)

            gt_mask_list.append(mask.squeeze().cpu().detach().numpy())
            gt_list.extend(y.cpu().detach().numpy())
            segment_scores.append(final_score_map)

    gt_list = np.array(gt_list)
    gt_mask_list = np.asarray(gt_mask_list)
    gt_mask_list = (gt_mask_list > 0).astype(np.int_)

    segment_scores = np.array(segment_scores)
    image_scores = np.array(image_scores)

    segment_scores = (segment_scores - segment_scores.min()) / (
        segment_scores.max() - segment_scores.min()
    )
    image_scores = (image_scores - image_scores.min()) / (
        image_scores.max() - image_scores.min()
    )

    img_roc_auc_det = roc_auc_score(gt_list, image_scores)
    print(f"{args.obj} AUC : {round(img_roc_auc_det,4)}")

    if CLASS_INDEX[args.obj] > 0:
        seg_roc_auc = roc_auc_score(gt_mask_list.flatten(), segment_scores.flatten())
        print(f"{args.obj} pAUC : {round(seg_roc_auc,4)}")
        return seg_roc_auc + img_roc_auc_det
    else:
        return img_roc_auc_det


if __name__ == "__main__":
    main()
