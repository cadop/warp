import os
import sys
import subprocess

import warp as wp


wp.init()

function_ref = open("docs/modules/functions.rst","w")

wp.print_builtins(function_ref)

function_ref.close()

# run Sphinx build
try:
    if os.name == 'nt':
        subprocess.check_output("make.bat html", cwd="docs", shell=True)
    else:
        subprocess.check_output("make html", cwd="docs", shell=True)
except subprocess.CalledProcessError as e:
    print(e.output.decode())
    raise(e)

print("Finished")
