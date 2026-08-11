"""
Static audit: every X.attr reference in the firmware must resolve.

An AttributeError on a class produces exactly the message
"type object 'X' has no attribute 'Y'", which matches the truncated
"type obj..." the operator saw over USB.
"""

import ast
import os
import sys
import types

import test_firmware as fw  # installs the machine stub + sys.path

FIRMWARE = fw.FIRMWARE


def load(name):
    return __import__(name)


def main():
    os.chdir(FIRMWARE)

    import config
    import sample_analysis
    import sample_store
    import carousel
    import mg995
    import database
    import as7265x

    modules = {
        "config": config,
        "analysis": sample_analysis,
        "sample_analysis": sample_analysis,
        "sample_store": sample_store,
        "carousel": carousel,
        "mg995": mg995,
        "database": database,
        "as7265x": as7265x,
    }

    classes = {
        "AS7265X": as7265x.AS7265X,
        "AS7265X_Driver": as7265x.AS7265X_Driver,
        "SoilMeasurementSystem": as7265x.SoilMeasurementSystem,
        "MG995": mg995.MG995,
        "Carousel": carousel.Carousel,
        "MaterialDatabase": database.MaterialDatabase,
        "SampleStore": sample_store.SampleStore,
        "CarouselError": carousel.CarouselError,
        "StorageError": sample_store.StorageError,
    }

    targets = dict(modules)
    targets.update(classes)

    problems = []

    for filename in sorted(os.listdir(FIRMWARE)):
        if not filename.endswith(".py") or filename == "wipe.py":
            continue

        src = open(os.path.join(FIRMWARE, filename), encoding="utf-8").read()

        try:
            tree = ast.parse(src)
        except SyntaxError as error:
            problems.append("{}: syntax error {}".format(filename, error))
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue

            if not isinstance(node.value, ast.Name):
                continue

            owner = node.value.id

            if owner not in targets:
                continue

            obj = targets[owner]

            if not hasattr(obj, node.attr):
                problems.append(
                    "{}:{}  {}.{}  DOES NOT EXIST".format(
                        filename, node.lineno, owner, node.attr
                    )
                )

    print("=" * 62)
    print(" ATTRIBUTE AUDIT")
    print("=" * 62)

    if problems:
        for p in problems:
            print("  " + p)
        print()
        print("{} problem(s) found".format(len(problems)))
    else:
        print("  all module/class attribute references resolve")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
