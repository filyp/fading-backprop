# %%
import matplotlib.pyplot as plt
import torch as pt
from transformers import AutoModelForCausalLM, AutoTokenizer
from utils import device, forward, get_perplexity, load_one_oscar_shard, get_norm_of_weights_change

from fading_backprop import (
    normal_train_step,
    scale_perturbation,
)

# params
batch_size = 32
model_id = "Qwen/Qwen2.5-0.5B"

# load datasets
tokenizer = AutoTokenizer.from_pretrained(model_id)
target_dataset = load_one_oscar_shard("pl", tokenizer)
retain_dataset = load_one_oscar_shard("en", tokenizer)

# %%
# load model
model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=pt.bfloat16)
model.to(device)

# create dataset iterators
target_unlearn_iter = iter(target_dataset["unlearn"].batch(batch_size))
target_relearn_iter = iter(target_dataset["relearn"].batch(batch_size))
retain_relearn_iter = iter(retain_dataset["relearn"].batch(batch_size))


def get_modules():
    for module_name, module in model.named_modules():
        if "mlp" in module_name and "_proj" in module_name:
            yield module_name, module


# install activation saving hooks
def save_pre_activations(module, args, output):
    module.last_pre_activations = args[0].detach().clone()


for module_name, module in get_modules():
    module.register_forward_hook(save_pre_activations)


# prepare activation importance tensors
target_act_imps = {}
retain_act_imps = {}
target_counter = 0
retain_counter = 0
for module_name, module in get_modules():
    input_size = module.weight.shape[1]
    target_act_imps[module_name] = pt.zeros(input_size).to(device)
    retain_act_imps[module_name] = pt.zeros(input_size).to(device)

# %%
for i in range(100):
    print(".", end="")
    with pt.no_grad():
        forward(model, next(target_unlearn_iter))
    for module_name, module in get_modules():
        act = module.last_pre_activations
        act = act.abs()
        act = act.mean(axis=[0, 1])
        act /= act.max()
        target_act_imps[module_name] += act
    target_counter += 1

# %%
for i in range(100):
    print(".", end="")
    with pt.no_grad():
        forward(model, next(retain_relearn_iter))
    for module_name, module in get_modules():
        act = module.last_pre_activations
        act = act.abs()
        act = act.mean(axis=[0, 1])
        act /= act.max()
        retain_act_imps[module_name] += act
    retain_counter += 1

# %%
for module_name, module in get_modules():
    target_imp = target_act_imps[module_name] / target_counter
    retain_imp = retain_act_imps[module_name] / retain_counter

    rel_imp = target_imp / retain_imp
    cutoff = rel_imp.quantile(0.99)
    with pt.no_grad():
        module.weight[:, rel_imp > cutoff] = 0

# %%
# calculate initial perplexities
pt.cuda.empty_cache()
original_model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=pt.bfloat16)
original_model.to(device)

init_target_ppl = get_perplexity(original_model, target_dataset)
init_retain_ppl = get_perplexity(original_model, retain_dataset)
print("init_target_ppl", init_target_ppl)
print("init_retain_ppl", init_retain_ppl)

# %%
for i in range(10):

    normal_train_step(model, next(retain_relearn_iter), 0.0003)
    # scale_perturbation(model, original_model.state_dict(), 0.99)

    normal_train_step(model, next(target_relearn_iter), 0.0003)

    res = {
        "target": get_perplexity(model, target_dataset) - init_target_ppl,
        "retain": get_perplexity(model, retain_dataset) - init_retain_ppl,
        "norm": get_norm_of_weights_change(model, original_model.state_dict()),
    }
    print({k: f"{v:.2f}" for k, v in res.items()})
    pt.cuda.empty_cache()
    if i == 0:
        first_res = res
print("change: ", {k: f"{v - first_res[k]:.2f}" for k, v in res.items()})

# %%