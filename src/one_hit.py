# %% init things, which need to be run once
import matplotlib.pyplot as plt
import torch as pt
from peft import LoraConfig, TaskType
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer

from utils import *

model_id = "Qwen/Qwen2.5-0.5B"
pt.set_default_device("cuda")

# load datasets
tokenizer = AutoTokenizer.from_pretrained(model_id)
forget_set = load_one_oscar_shard("pl", tokenizer)
retain_set = load_one_oscar_shard("en", tokenizer)
forget_eval = get_batch(iter(forget_set["validation"]), 32)
retain_eval = get_batch(iter(retain_set["validation"]), 32)


# %%
# hyperparameters
quantile = 0.99  # between 0 and 1
circuit_name = "forget_linear_correct_logit"
criterion = 'c("retain_linear_correct_logit").abs() ** -1'
forget_lr = 1e-2
retain_lr = 4e-4

set_seeds(42)

# load circuit
circuit = c(circuit_name)
# sparsify circuit
for param_name, scores in kinda_safe_eval(criterion).items():
    # * now we calculate threshold per parameter, but we could also do it per model
    k = int(scores.numel() * quantile)
    threshold = scores.flatten().kthvalue(k).values
    circuit[param_name][scores < threshold] = 0

# load model
model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=pt.bfloat16)
# add lora
lora_config = LoraConfig(
    task_type=TaskType.SEQ_2_SEQ_LM,
    inference_mode=False,
    r=16,
    lora_alpha=32,
    lora_dropout=0.1,
    target_modules="all-linear",
)
model.add_adapter(lora_config, adapter_name="retain_lora")

# optimizer = pt.optim.Adam(model.parameters(), lr=retain_lr, betas=(0.9, 0.999))
optimizer = pt.optim.SGD(model.parameters(), lr=retain_lr)

# unlearning loop
retain_iter = looping_iter(retain_set["train"])
for step in range(1, 1 + 100):
    model.train()

    for name, param in model.named_parameters():
        name = name.replace(".base_layer", "")
        if "lora" in name:
            continue
        # if "_proj" in name:
        param.data -= circuit[name] * forget_lr

    # standard forward, backward, and update
    optimizer.zero_grad(set_to_none=True)
    input_ids = get_batch(retain_iter, 8)
    output = model(input_ids)
    loss = cross_entropy_loss(output, input_ids)
    loss.backward()
    optimizer.step()

    if step % 10 != 0:
        continue

    # evaluate
    f_ppl, r_ppl = get_perplexities(model, [forget_eval, retain_eval])
    stats = dict(forget=f_ppl, retain=r_ppl)
    # wandb.log(stats)
    print(f"{step:4d}  " + "   ".join(f"{v:10.2f}" for v in stats.values()))
