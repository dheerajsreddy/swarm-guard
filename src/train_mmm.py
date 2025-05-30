import torch
import torch.nn.functional as F
import typer
from sklearn.metrics import classification_report, roc_auc_score, confusion_matrix, average_precision_score
from tqdm import tqdm
from transformers import AutoModel

from src.dataset import AstroturfCampaignMultiModalDataset, astrorag_collate_fn
from src.helpers.device_helpers import move_to_device
from src.helpers.early_stopping import EarlyStopping
from src.helpers.model_loaders import load_pre_trained_graph_encoder
from src.modules.loss.focal_loss import FocalLoss
from src.modules.multi_modal.multi_modal_model import MultiModalModelForClassification

app = typer.Typer()


def load_data(
        train_dataset_root_dir: str,
        test_dataset_root_dir: str,
        validation_split: float,
        text_encoder_model_id: str,
        image_encoder_model_id: str,
        batch_size: int = 32
):
    train_dataset = AstroturfCampaignMultiModalDataset(
        json_dir=f"{train_dataset_root_dir}/graphs",
        image_dir=f"{train_dataset_root_dir}/images",
        text_model_id=text_encoder_model_id,
        vision_model_id=image_encoder_model_id,
    )
    val_size = int(len(train_dataset) * validation_split)
    train_size = len(train_dataset) - val_size
    train_dataset, val_dataset = torch.utils.data.random_split(
        train_dataset, [train_size, val_size]
    )
    test_dataset = AstroturfCampaignMultiModalDataset(
        json_dir=f"{test_dataset_root_dir}/graphs",
        image_dir=f"{test_dataset_root_dir}/images",
        text_model_id=text_encoder_model_id,
        vision_model_id=image_encoder_model_id,
    )
    train_data_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        collate_fn=astrorag_collate_fn,
    )
    val_data_loader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=batch_size,
        shuffle=False,
        collate_fn=astrorag_collate_fn,
    )
    test_data_loader = torch.utils.data.DataLoader(
        test_dataset,
        batch_size=batch_size,
        shuffle=False,
        collate_fn=astrorag_collate_fn,
    )
    return train_data_loader, val_data_loader, test_data_loader


def train_function(
        model,
        train_data_loader,
        val_data_loader,
        max_epochs: int = 10,
        patience: int = 5,
        lr: float = 1e-4,
        weight_decay: float = 1e-5,
        alpha: float = 1.0,
        gamma: float = 2.0,
):
    criterion = FocalLoss(alpha=alpha, gamma=gamma, reduction="mean")
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    early_stopping = EarlyStopping(patience=patience, verbose=True, mode="max")
    device = "cuda" if torch.cuda.is_available() else "cpu"

    for epoch in range(max_epochs):
        model.train()
        train_loss = 0.0
        for batch in tqdm(train_data_loader, desc=f"Training Epoch {epoch + 1}/{max_epochs}"):
            text_input_ids = batch["text_input_ids"].to(device)
            text_attention_mask = batch["text_attention_mask"].to(device)
            graph_data = batch["graph_data"]
            pixel_values = batch["pixel_values"].to(device)
            graph_data.x = graph_data.x.to(device)
            graph_data.edge_index = graph_data.edge_index.to(device)
            graph_data.batch = graph_data.batch.to(device)
            labels = batch["labels"].to(device)

            optimizer.zero_grad()
            outputs = model(text_input_ids, text_attention_mask, graph_data, pixel_values)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            train_loss += loss.item()

        train_loss /= len(train_data_loader)

        model.eval()
        val_loss = 0.0
        all_labels = []
        all_scores = []
        with torch.no_grad():
            for batch in val_data_loader:
                text_input_ids = batch["text_input_ids"].to(device)
                text_attention_mask = batch["text_attention_mask"].to(device)
                graph_data = batch["graph_data"]
                graph_data.x = graph_data.x.to(device)
                graph_data.edge_index = graph_data.edge_index.to(device)
                graph_data.batch = graph_data.batch.to(device)
                labels = batch["labels"].to(device)

                outputs = model(text_input_ids, text_attention_mask, graph_data)
                loss = criterion(outputs, labels)
                val_loss += loss.item()

                probs = F.softmax(outputs, dim=1)
                scores = probs[:, 1]
                all_scores.extend(scores.cpu().numpy())
                all_labels.extend(labels.cpu().numpy())

        val_loss /= len(val_data_loader)
        roc_auc = roc_auc_score(all_labels, all_scores)
        pr_auc = average_precision_score(all_labels, all_scores)
        print(
            f"Epoch {epoch + 1}/{max_epochs}, "
            f"Train Loss: {train_loss:.4f}, "
            f"Val Loss: {val_loss:.4f}, "
            f"Val ROC AUC: {roc_auc:.4f}, "
            f"Val PR AUC: {pr_auc:.4f}"
        )

        early_stopping(pr_auc, model)
        if early_stopping.early_stop:
            print("Early stopping")
            break


def save_model_with_config(model, config, path):
    torch.save({"model_state_dict": model.state_dict(), "config": config}, path)


def run_test(model, test_data_loader, test_results_output_path):
    model.eval()
    all_preds = []
    all_labels = []
    all_scores = []
    device = "cuda" if torch.cuda.is_available() else "cpu"
    with torch.no_grad():
        for batch in test_data_loader:
            text_input_ids = batch["text_input_ids"].to(device)
            text_attention_mask = batch["text_attention_mask"].to(device)
            graph_data = batch["graph_data"]
            graph_data.x = graph_data.x.to(device)
            graph_data.edge_index = graph_data.edge_index.to(device)
            graph_data.batch = graph_data.batch.to(device)

            outputs = model(text_input_ids, text_attention_mask, graph_data)
            probs = F.softmax(outputs, dim=1)
            scores = probs[:, 1]
            preds = torch.argmax(outputs, dim=1)

            all_scores.extend(scores.cpu().numpy())
            all_preds.extend(preds.cpu().numpy())
            all_labels.extend(batch["labels"].cpu().numpy())

    report = classification_report(all_labels, all_preds, output_dict=True)
    roc_auc = roc_auc_score(all_labels, all_scores)
    pr_auc = average_precision_score(all_labels, all_scores)
    cm = confusion_matrix(all_labels, all_preds)

    print("Classification Report:")
    print(report)
    print(f"ROC AUC Score: {roc_auc:.4f}")
    print(f"PR AUC Score: {pr_auc:.4f}")
    print("Confusion Matrix:", cm)

    with open(test_results_output_path, "w") as f:
        f.write("Classification Report:\n")
        f.write(str(report))
        f.write(f"\nROC AUC Score:\n{roc_auc:.4f}")
        f.write(f"\nPR AUC Score:\n{pr_auc:.4f}")
        f.write("\nConfusion Matrix:\n")
        f.write(str(cm))


@app.command()
def main(
        graph_encoder_model_path: str = typer.Option(
            default="model/graph_encoder.pt", help="Path to the graph encoder model."
        ),
        text_encoder_model_id: str = typer.Option(
            default="answerdotai/ModernBERT-base", help="Model ID for the text encoder."
        ),
        image_encoder_model_id: str = typer.Option(
            default="google/vit-base-patch16-224", help="Model ID for the image encoder."
        ),
        train_dataset_root_dir: str = typer.Option(
            default="dataset1", help="Root directory of the training dataset."
        ),
        test_dataset_root_dir: str = typer.Option(
            default="dataset2", help="Root directory of the test dataset."
        ),
        self_attention_heads: int = typer.Option(
            default=4, help="Number of self-attention heads."
        ),
        embedding_dim: int = typer.Option(
            default=512, help="Embedding dimension."
        ),
        self_attn_ff_dim: int = typer.Option(
            default=512, help="Feed-forward dimension for self-attention."
        ),
        num_cross_modal_attention_blocks: int = typer.Option(
            default=4, help="Number of cross-modal attention blocks."
        ),
        num_cross_modal_attention_heads: int = typer.Option(
            default=4, help="Number of cross-modal attention heads."
        ),
        num_cross_modal_attention_ff_dim: int = typer.Option(
            default=512, help="Feed-forward dimension for cross-modal attention."
        ),
        batch_size: int = typer.Option(default=32, help="Batch size for training."),
        lr: float = typer.Option(default=1e-4, help="Learning rate."),
        weight_decay: float = typer.Option(default=1e-5, help="Weight decay for optimizer."),
        max_epochs: int = typer.Option(default=100, help="Maximum number of epochs for training."),
        validation_split: float = typer.Option(default=0.2, help="Validation split ratio."),
        alpha: float = typer.Option(default=1.0, help="Alpha parameter for focal loss."),
        gamma: float = typer.Option(default=2.0, help="Gamma parameter for focal loss."),
        model_output_path: str = typer.Option(
            default="model/multi_modal_model.pt", help="Path to save the trained model."
        ),
        test_results_output_path: str = typer.Option(
            default="model/test_results.txt", help="Path to save the test results."
        ),
):
    text_encoder = AutoModel.from_pretrained(text_encoder_model_id)
    vision_encoder = AutoModel.from_pretrained(image_encoder_model_id)
    graph_encoder = load_pre_trained_graph_encoder(
        model_path=graph_encoder_model_path,
        device="cuda" if torch.cuda.is_available() else "cpu",
    )

    train_loader, val_loader, test_loader = load_data(
        train_dataset_root_dir=train_dataset_root_dir,
        test_dataset_root_dir=test_dataset_root_dir,
        validation_split=validation_split,
        text_encoder_model_id=text_encoder_model_id,
        image_encoder_model_id=image_encoder_model_id,
        batch_size=batch_size,
    )

    model = MultiModalModelForClassification(
        text_encoder=text_encoder,
        graph_encoder=graph_encoder,
        vision_encoder=vision_encoder,
        self_attention_heads=self_attention_heads,
        embedding_dim=embedding_dim,
        num_cross_modal_attention_blocks=num_cross_modal_attention_blocks,
        num_cross_modal_attention_heads=num_cross_modal_attention_heads,
        self_attn_ff_dim=self_attn_ff_dim,
        num_cross_modal_attention_ff_dim=num_cross_modal_attention_ff_dim,
        output_channels=2,
    )
    move_to_device(model)
    train_function(
        model=model,
        train_data_loader=train_loader,
        val_data_loader=val_loader,
        max_epochs=max_epochs,
        patience=5,
        lr=lr,
        weight_decay=weight_decay,
        alpha=alpha,
        gamma=gamma,
    )
    save_model_with_config(
        model=model,
        config={
            "self_attention_heads": self_attention_heads,
            "embedding_dim": embedding_dim,
            "num_cross_modal_attention_blocks": num_cross_modal_attention_blocks,
            "num_cross_modal_attention_heads": num_cross_modal_attention_heads,
            "self_attn_ff_dim": self_attn_ff_dim,
            "num_cross_modal_attention_ff_dim": num_cross_modal_attention_ff_dim,
            "alpha": alpha,
            "gamma": gamma,
        },
        path=model_output_path,
    )
    run_test(
        model=model,
        test_data_loader=test_loader,
        test_results_output_path=test_results_output_path,
    )


if __name__ == "__main__":
    app()
