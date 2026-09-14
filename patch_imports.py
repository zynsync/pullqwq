import os
import re

ROOT = '/workspace/Kazumi/lib'

IO_IMPORT = re.compile(r"^import 'dart:io';\s*$", re.MULTILINE)
PATH_IMPORT = re.compile(
    r"^import 'package:path_provider/path_provider\.dart';\s*$", re.MULTILINE
)

IO_REPLACEMENT = (
    "import 'package:kazumi/web_stubs/io_stub.dart'\n"
    "    if (dart.library.io) 'dart:io';"
)

PATH_REPLACEMENT = (
    "import 'package:kazumi/web_stubs/path_stub.dart'\n"
    "    if (dart.library.io) 'package:path_provider/path_provider.dart';"
)

changed = 0
for dirpath, _, filenames in os.walk(ROOT):
    for name in filenames:
        if not name.endswith('.dart'):
            continue
        path = os.path.join(dirpath, name)
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
        out = IO_IMPORT.sub(IO_REPLACEMENT, src)
        out = PATH_IMPORT.sub(PATH_REPLACEMENT, out)
        if out != src:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(out)
            changed += 1

print(f'rewrote {changed} files')
