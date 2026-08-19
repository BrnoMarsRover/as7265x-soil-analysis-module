"""
Group-aware validation splits.

THE MISTAKE THIS PREVENTS

Three acquisitions of one undisturbed sample are nearly identical. Split
them at random and two land in training, one in test, and the model
scores 100% for recognising a spectrum it has effectively already seen.
That number is not accuracy, it is memorisation, and it is the single
easiest way to build a system that looks excellent and fails in the
field. §33.

So splits are by GROUP, never by measurement. A group is, in order of
preference:

    sample_group    the physical sample - the strongest grouping
    session_id      everything measured in one sitting, sharing one
                    calibration, one warm-up and one operator
    measurement_id  the fallback when nothing else is recorded

LEAVE-ONE-CLASS-OUT IS ALSO PROVIDED, AND IT IS NOT OPTIONAL FOR TRANSFER

For a calibration-transfer model the question is "does this mapping help
on a material it was not fitted on", so whole MATERIALS are held out.
Splitting by measurement there would let the mapping memorise the very
pairs it is being judged on. §29.

NO FITTING OUTSIDE A FOLD

Anything learned - scaling, centring, feature selection, covariance,
class statistics, probability calibration - must be computed inside the
training fold. `Fold` carries only ids, so the caller cannot accidentally
pass a statistic fitted on everything. §34.
"""


class Fold:
    """One split. Ids only, so nothing pre-fitted can leak in with it."""

    def __init__(self, index, train_ids, test_ids, held_out):
        self.index = index
        self.train_ids = list(train_ids)
        self.test_ids = list(test_ids)
        self.held_out = held_out

    def as_dict(self):
        return {
            "index": self.index,
            "held_out": self.held_out,
            "train": sorted(self.train_ids),
            "test": sorted(self.test_ids),
            "train_size": len(self.train_ids),
            "test_size": len(self.test_ids),
        }


def group_of(observation):
    """The strongest grouping this observation supports."""
    return (
        observation.get("sample_group")
        or observation.get("session_id")
        or observation["measurement_id"]
    )


def group_observations(observations):
    grouped = {}

    for observation in observations:
        grouped.setdefault(group_of(observation), []).append(
            observation["measurement_id"]
        )

    return grouped


def leave_one_group_out(observations):
    """One fold per physical sample. The default for classifier work."""
    grouped = group_observations(observations)
    folds = []

    for index, (group, ids) in enumerate(sorted(grouped.items())):
        train = [
            measurement_id
            for other, members in grouped.items() if other != group
            for measurement_id in members
        ]

        folds.append(Fold(index, train, ids, group))

    return folds


def leave_one_class_out(observations, label_of=None):
    """One fold per material. The right split for transfer models."""
    label_of = label_of or (
        lambda observation: observation.get("material_key")
    )

    by_class = {}

    for observation in observations:
        by_class.setdefault(label_of(observation), []).append(
            observation["measurement_id"]
        )

    folds = []

    for index, (label, ids) in enumerate(sorted(by_class.items())):
        train = [
            measurement_id
            for other, members in by_class.items() if other != label
            for measurement_id in members
        ]

        folds.append(Fold(index, train, ids, label))

    return folds


def feasibility(observations, label_of=None):
    """
    Can this dataset support supervised validation at all?

    Answered before any training runs, because the answer today is no and
    saying so plainly is worth more than a benchmark table full of zeros
    that somebody later quotes as "the model's accuracy". §62.
    """
    label_of = label_of or (
        lambda observation: observation.get("material_key")
    )

    by_class = {}

    for observation in observations:
        by_class.setdefault(label_of(observation), []).append(observation)

    groups_per_class = {
        label: len({group_of(entry) for entry in entries})
        for label, entries in by_class.items()
    }

    single = sorted(
        label for label, count in groups_per_class.items() if count < 2
    )

    return {
        "observations": len(observations),
        "classes": len(by_class),
        "groups": len(group_observations(observations)),
        "groups_per_class": groups_per_class,
        "classes_with_one_group": single,
        "supervised_validation_possible": not single,
        "reason": (
            "Every class has at least two independent groups, so a class "
            "can be held out and still be learnable from the rest."
            if not single else
            "{} class(es) have a single independent measurement: "
            "{}. Holding one out removes the class from training "
            "entirely, so leave-one-out accuracy would be zero by "
            "construction and would measure the dataset, not the model. "
            "A second independent measurement of each material is what "
            "unblocks this.".format(len(single), ", ".join(single[:6]))
        ),
    }
