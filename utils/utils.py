import time
import inspect

import torch
from loguru import logger


def to_device(x, device):
    if not isinstance(x, dict):
        return x

    new_x = {}

    for k, v in x.items():
        if isinstance(v, torch.Tensor):
            new_v = v.to(device)
        elif isinstance(v, (tuple, list)) and len(v) > 0 and isinstance(v[0], torch.Tensor):
            new_v = [i.to(device) for i in v]
        else:
            new_v = v

        new_x[k] = new_v

    return new_x


def aggregate_dict(x):
    """Aggregate a list of dict to form a new dict"""
    agg_x = {}

    for ele in x:
        assert isinstance(ele, dict)

        for k, v in ele.items():
            if k not in agg_x:
                agg_x[k] = []

            if isinstance(v, (tuple, list)):
                agg_x[k].extend(list(v))
            else:
                agg_x[k].append(v)

    # Stack if possible
    new_agg_x = {}
    for k, v in agg_x.items():
        try:
            v = torch.cat(v, dim=0)
        except Exception:
            pass
        new_agg_x[k] = v

    return new_agg_x


def raise_or_warn(action, msg):
    if action == "raise":
        raise ValueError(msg)
    else:
        logger.warning(msg)


class ConfigComparer:
    """Compare two config dictionaries. Useful for checking when resuming from
    previous session."""

    _to_raise_error = [
        "model->model_name_or_path"
    ]
    _to_warn = [
        "model->config_name", "model->tokenizer_name", "model->cache_dir", "model->freeze_base_model", "model->fusion",
        "model->lambdas"
    ]

    def __init__(self, cfg_1, cfg_2):
        self.cfg_1 = cfg_1
        self.cfg_2 = cfg_2

    def compare(self):
        for components, action in \
                [(self._to_raise_error, "raise"), (self._to_warn, "warn")]:
            for component in components:
                curr_scfg_1, curr_scfg_2 = self.cfg_1, self.cfg_2  # subconfigs
                for key in component.split("->"):
                    if key not in curr_scfg_1 or key not in curr_scfg_2:
                        raise ValueError(
                            f"Component {component} not found in config file.")
                    curr_scfg_1 = curr_scfg_1[key]
                    curr_scfg_2 = curr_scfg_2[key]
                if curr_scfg_1 != curr_scfg_2:
                    msg = (f"Component {component} is different between "
                           f"two config files\nConfig 1: {curr_scfg_1}\n"
                           f"Config 2: {curr_scfg_2}.")
                    raise_or_warn(action, msg)
        return True


def collect(config, args, collected):
    """Recursively collect each argument in `args` from `config` and write to
    `collected`."""
    if not isinstance(config, dict):
        return

    keys = list(config.keys())
    for arg in args:
        if arg in keys:
            if arg in collected:  # already collected
                raise RuntimeError(f"Found repeated argument: {arg}")
            collected[arg] = config[arg]

    for key, sub_config in config.items():
        collect(sub_config, args, collected)


def from_config(main_args=None, requires_all=False):
    """Wrapper for all classes, which wraps `__init__` function to take in only
    a `config` dict, and automatically collect all arguments from it. An error
    is raised when duplication is found. Note that keyword arguments are still
    allowed, in which case they won't be collected from `config`.

    Parameters
    ----------
    main_args : str
        If specified (with "a->b" format), arguments will first be collected
        from this subconfig. If there are any arguments left, recursively find
        them in the entire config. Multiple main args are to be separated by
        ",".
    requires_all : bool
        Whether all function arguments must be found in the config.
    """
    global_main_args = main_args
    if global_main_args is not None:
        global_main_args = global_main_args.split(",")
        global_main_args = [args.split("->") for args in global_main_args]

    def decorator(init):
        init_args = inspect.getfullargspec(init)[0][1:]  # excluding self

        def wrapper(self, config=None, main_args=None, **kwargs):
            # Add config to self
            if config is not None:
                self.config = config

            # Get config from self
            elif getattr(self, "config", None) is not None:
                config = self.config

            if main_args is None:
                main_args = global_main_args
            else:
                # Overwrite global_main_args
                main_args = main_args.split(",")
                main_args = [args.split("->") for args in main_args]

            collected = kwargs  # contains keyword arguments
            not_collected = [arg for arg in init_args if arg not in collected]
            # Collect from main args
            if config is not None and main_args is not None \
                    and len(not_collected) > 0:
                for main_arg in main_args:
                    sub_config = config
                    for arg in main_arg:
                        if arg not in sub_config:
                            break  # break when `main_args` is invalid
                        sub_config = sub_config[arg]
                    else:
                        collect(sub_config, not_collected, collected)
                    not_collected = [arg for arg in init_args
                                     if arg not in collected]
                    if len(not_collected) == 0:
                        break
            # Collect from the rest
            not_collected = [arg for arg in init_args if arg not in collected]
            if config is not None and len(not_collected) > 0:
                collect(config, not_collected, collected)
            # Validate
            if requires_all and (len(collected) < len(init_args)):
                not_collected = [arg for arg in init_args
                                 if arg not in collected]
                raise RuntimeError(
                    f"Found missing argument(s) when initializing "
                    f"{self.__class__.__name__} class: {not_collected}.")
            # Call function
            return init(self, **collected)
        return wrapper
    return decorator


class Timer:
    def __init__(self):
        self.global_start_time = time.time()
        self.start_time = None
        self.last_interval = None
        self.accumulated_interval = None

    def start(self):
        assert self.start_time is None
        self.start_time = time.time()

    def end(self):
        assert self.start_time is not None
        self.last_interval = time.time() - self.start_time
        self.start_time = None

        # Update accumulated interval
        if self.accumulated_interval is None:
            self.accumulated_interval = self.last_interval
        else:
            self.accumulated_interval = (
                0.9 * self.accumulated_interval + 0.1 * self.last_interval)

    def get_last_interval(self):
        return self.last_interval

    def get_accumulated_interval(self):
        return self.accumulated_interval

    def get_total_time(self):
        return time.time() - self.global_start_time


def compute_metrics_from_inputs_and_outputs(inputs, outputs, tokenizer, confidence_threshold=0.5, save_csv_path=None):
    input_ids = inputs["input_ids"]

    # Groundtruths
    poi_start_gt, poi_end_gt = inputs["poi_start"], inputs["poi_end"]
    street_start_gt, street_end_gt = inputs["street_start"], inputs["street_end"]
    # Stack
    poi_span_gt = torch.stack([poi_start_gt, poi_end_gt], dim=-1)  # (B, 2)
    street_span_gt = torch.stack([street_start_gt, street_end_gt], dim=-1)  # (B, 2)

    # Predictions
    poi_span_preds, street_span_preds = outputs["poi_span_preds"], outputs["street_span_preds"]  # (B, L, 2)
    poi_span_preds = poi_span_preds.argmax(dim=1)  # (B, 2)
    street_span_preds = street_span_preds.argmax(dim=1)  # (B, 2)

    poi_existence_preds, street_existence_preds = outputs["poi_existence_preds"], outputs["street_existence_preds"]  # (B,)
    has_poi_preds = (poi_existence_preds >= confidence_threshold)  # (B,)
    has_street_preds = (street_existence_preds >= confidence_threshold)  # (B,)

    # Mask predictions that model is not confident about by -1 (i.e., not present)
    negative_one = torch.tensor(-1).to(poi_span_preds)
    poi_span_preds_masked = torch.where(has_poi_preds.unsqueeze(-1), poi_span_preds, negative_one)  # (B, 2)
    street_span_preds_masked = torch.where(has_street_preds.unsqueeze(-1), street_span_preds, negative_one)  # (B, 2)

    # Calculate accuracy
    poi_span_correct = (poi_span_gt.int() == poi_span_preds_masked.int()).all(-1)  # (B,)
    poi_span_acc = poi_span_correct.sum() / float(len(poi_span_correct))  # scalar

    street_span_correct = (street_span_gt.int() == street_span_preds_masked.int()).all(-1)  # (B,)
    street_span_acc = street_span_correct.sum() / float(len(street_span_correct))  # scalar

    total_correct = (poi_span_correct & street_span_correct)  # (B,)
    total_acc = total_correct.sum() / float(len(total_correct))  # scalar

    acc = {"total_acc": total_acc, "poi_acc": poi_span_acc, "street_acc": street_span_acc}

    # Generate prediction csv if needed
    if save_csv_path is not None:
        return

    return acc
