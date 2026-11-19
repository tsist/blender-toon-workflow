# Toon Workflow

Blender add-on for cel shading, editable line rendering, and compound EEVEE output. Version **0.52.1** is the initial public source release. The installable ZIP contains project-authored Python source and configuration only. Example characters, project files, textures, and art references are omitted.

## Install

In Blender 5.3.0, choose **Edit → Preferences → Add-ons → Install from Disk** and enable **三渲二 · Toon Workflow**. The release was tested on Windows x64 build `02f0899d78e3` with a separate profile. Open a saved copy when using the upgrade action on an existing project.

Rebuild the release ZIP locally with `python build.py toon_workflow-0.52.1.zip`. The build verifies each source file against the package manifest.

The technical qualification covers installation, cold startup, upgrade-copy and reopen, plus Beauty, Line and Combined from a generated fixture. The optional `toon_workflow_cli_v1` adapter uses an external compatible blenderctl core with explicit installation identity. The installable UI package can register without that optional host runner. Character style tuning and interactive Vulkan line output have separate qualification.

The source and embedded line modules are released under GPL-3.0-or-later. See the retained licence and line-engine attribution notices.
