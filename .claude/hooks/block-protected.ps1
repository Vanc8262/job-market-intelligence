# PreToolUse guard: exit 2 blocks the edit. A crash must exit non-zero, or onFailure:block never fires.
$ErrorActionPreference = 'Stop'

# Matched against the path from the repo root and against the file name.
$protected = '.env', '.env.*', '.git/*', 'data/private/*', 'data/application_tracking/*',
    'config/resume_profile.local.yaml'
$allowed = '.env.example'

$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$tool = ([Console]::In.ReadToEnd() | ConvertFrom-Json).tool_input
$target = if ($tool.file_path) { $tool.file_path } else { $tool.path }  # Desktop Commander write_file uses path
if (-not $target) { throw 'No file path in tool input' }

$full = [IO.Path]::GetFullPath([IO.Path]::Combine($root, $target))  # resolves .. and mixed slashes
if (-not $full.StartsWith("$root\", [StringComparison]::OrdinalIgnoreCase)) { exit 0 }
$rel = $full.Substring($root.Length + 1).Replace('\', '/')
$leaf = Split-Path $full -Leaf

if ($allowed | Where-Object { $leaf -like $_ }) { exit 0 }
foreach ($pattern in $protected) {
    if ($rel -like $pattern -or $leaf -like $pattern) {
        [Console]::Error.WriteLine("Blocked: $rel is protected ($pattern). Ask the owner to change it.")
        exit 2
    }
}
exit 0
