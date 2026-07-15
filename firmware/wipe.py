# wipe.py
# Deletes all files and directories from ESP32 filesystem.

import os


def remove_tree(path):
    for name in os.listdir(path):
        if path == "/":
            full_path = "/" + name
        else:
            full_path = path + "/" + name

        try:
            os.remove(full_path)
            print("deleted file:", full_path)

        except OSError:
            remove_tree(full_path)
            os.rmdir(full_path)
            print("deleted directory:", full_path)


remove_tree("/")
print("ESP32 filesystem cleaned")