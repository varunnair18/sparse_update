import torch
import torch.nn.functional as F
from transformers import BertForSequenceClassification
from pytorch_lightning import LightningModule
from .register import register
from datasets import load_metric
from sparse_update.utilities.optimization import get_scheduler

# Map the module name to file name (.tsv file)
FILE_NAME_MAP = {"sst2": "SST-2"}


@register
class SST2Module(LightningModule):
    """
    LightningModule for the sst2 dataset
    """

    name = "sst2"

    def __init__(self, args, bert_config):
        """
        Args:
            args: the config storing the hyperparameters
            bert_config: config name for Huggingface BERT models

        """
        super().__init__()
        self.args = args
        self.model = BertForSequenceClassification.from_pretrained(bert_config)

    def on_train_epoch_start(self):
        # Initialize the metric module every epoch
        self.train_metric = load_metric("glue", self.name)

    def on_validation_epoch_start(self):
        # Initialize the metric module every epoch
        self.val_metric = load_metric("glue", self.name)

    def on_test_epoch_start(self):
        # Initialize the metric module every epoch
        self.test_metric = load_metric("glue", self.name)

    def shared_step(self, batch, batch_idx, metric, mode="train"):
        input_ids, attention_mask, token_type_ids, labels = batch

        # Truncate the data to maximum length within this batch
        # to save memories and speed up traning
        max_len = attention_mask.sum(-1).max()

        input_ids = input_ids[:, :max_len]
        attention_mask = attention_mask[:, :max_len]
        token_type_ids = token_type_ids[:, :max_len]

        # Compute the loss and logits
        return_dict = self.model(
            input_ids, attention_mask, token_type_ids, return_dict=True, labels=labels
        )

        self.log(f"{mode}/loss", return_dict["loss"])

        # Save the computed metrics for this batch
        predictions = torch.argmax(return_dict["logits"], -1)

        metric.add_batch(predictions=predictions, references=labels)

        return {"loss": return_dict["loss"]}

    def training_step(self, batch, batch_idx):
        # Moniter the learning rate decreasing correctly
        self.log("lr", self.trainer.lr_schedulers[0]["scheduler"].get_last_lr()[0])

        return self.shared_step(batch, batch_idx, self.train_metric, "train")

    def validation_step(self, batch, batch_idx):
        return self.shared_step(batch, batch_idx, self.val_metric, "val")

    def test_step(self, batch, batch_idx):
        input_ids, attention_mask, token_type_ids, labels = batch

        # Truncate the data to maximum length within this batch
        # to save memories and speed up traning
        max_len = attention_mask.sum(-1).max()

        input_ids = input_ids[:, :max_len]
        attention_mask = attention_mask[:, :max_len]
        token_type_ids = token_type_ids[:, :max_len]

        # Only compute the logits for test set
        return_dict = self.model(
            input_ids, attention_mask, token_type_ids, return_dict=True
        )

        # Store the predictions
        predictions = torch.argmax(return_dict["logits"], -1)

        return {"predictions": predictions}

    def shared_epoch_end(self, outputs, metric, mode="train"):
        # metric.compute() will calculate the overal accuracy for the whole set
        acc = metric.compute()["accuracy"]

        self.log(f"{mode}/acc", acc)

    def training_epoch_end(self, outputs):
        self.shared_epoch_end(outputs, self.train_metric, "train")

    def validation_epoch_end(self, outputs):
        self.shared_epoch_end(outputs, self.val_metric, "val")

    def test_epoch_end(self, outputs):
        # After testing, we will extract all the predictions
        # and output them to a .tsv file. This file can be used
        # to test our model on glue server. Please refer to
        # https://gluebenchmark.com/faq for more details.
        predictions = torch.cat([o["predictions"] for o in outputs], 0)

        out = "index\tprediction\n"

        predictions = predictions.cpu().numpy().tolist()
        indices = list(range(len(predictions)))

        for i, p in zip(indices, predictions):
            out += f"{i}\t{p}\n"

        file_name = f"{FILE_NAME_MAP[self.name]}.tsv"
        with open(file_name, "w") as record_file:
            record_file.write(out)

    def configure_optimizers(self):
        # Separate the parameters into two groups. One used weight decay
        # while the other don't. Codes borrow from https://github.com/\
        # huggingface/transformers/blob/5f80c15ef53b4c2c10eeec64b2e42e62db130930/\
        # src/transformers/trainer.py#L576-L585

        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [
                    p
                    for n, p in self.named_parameters()
                    if not any(nd in n for nd in no_decay)
                ],
                "weight_decay": self.args.wd,
            },
            {
                "params": [
                    p
                    for n, p in self.named_parameters()
                    if any(nd in n for nd in no_decay)
                ],
                "weight_decay": 0.0,
            },
        ]

        num_training_steps = len(self.train_dataloader()) * self.args.max_epochs

        # num_training_steps = self.args.max_epochs

        optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=self.args.lr)

        # Get the scheduler according the argument
        scheduler_func = get_scheduler(self.args.lr_scheduler_type)
        scheduler = scheduler_func(
            optimizer, self.args.num_warmup_steps, num_training_steps, last_epoch=-1
        )

        # Remember to set `interval` to `step`, so that the scheduler will update
        # learning rate every step. The other option is `epoch`.
        scheduler = {
            "scheduler": scheduler,
            "monitor": "metric_to_track",
            "interval": "step",
            "frequency": 1,
            "strict": True,
        }

        return [optimizer], [scheduler]
