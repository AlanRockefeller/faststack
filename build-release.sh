#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./build-release.sh [--tag v1.6.4-build3] [--target origin/main] [--dry-run] [--skip-ruff]

Build FastStack release binaries by creating and pushing a v* tag.
GitHub Actions builds the Windows and macOS artifacts and publishes them to
the GitHub Release for that tag.

Defaults:
  --target origin/main
  --tag    next unused v<pyproject version>-buildN tag

Examples:
  ./build-release.sh
  ./build-release.sh --tag v1.6.5-build1
  ./build-release.sh --dry-run
USAGE
}

die() {
  echo "error: $*" >&2
  exit 1
}

note() {
  echo "==> $*"
}

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git repository"
cd "$repo_root"

tag=""
target_ref="origin/main"
dry_run=false
skip_ruff=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tag)
      [ "$#" -ge 2 ] || die "--tag requires a value"
      tag="$2"
      shift 2
      ;;
    --target)
      [ "$#" -ge 2 ] || die "--target requires a value"
      target_ref="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    --skip-ruff)
      skip_ruff=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

version="$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -n 1)"
[ -n "$version" ] || die "could not read project version from pyproject.toml"

find_venv_python() {
  local candidate
  for candidate in \
    ".venv/Scripts/python.exe" \
    ".venv/bin/python" \
    "venv/Scripts/python.exe" \
    "venv/bin/python" \
    ".venv-win/Scripts/python.exe"
  do
    if [ -x "$candidate" ] || [ -f "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

if [ -n "$(git status --porcelain)" ]; then
  die "working tree has uncommitted changes; commit or stash before building a release"
fi

git remote get-url origin >/dev/null 2>&1 || die "remote 'origin' is not configured"

note "Fetching origin/main and tags"
git fetch origin +main:refs/remotes/origin/main --tags

git rev-parse --verify "$target_ref^{commit}" >/dev/null 2>&1 \
  || die "target ref does not resolve to a commit: $target_ref"

if [ -z "$tag" ]; then
  prefix="v${version}-build"
  max_build=0
  while IFS= read -r existing_tag; do
    case "$existing_tag" in
      ${prefix}[0-9]*)
        build_number="${existing_tag#$prefix}"
        case "$build_number" in
          ''|*[!0-9]*) continue ;;
        esac
        if [ "$build_number" -gt "$max_build" ]; then
          max_build="$build_number"
        fi
        ;;
    esac
  done < <(git tag --list "${prefix}*" | sort -u)
  tag="${prefix}$((max_build + 1))"
fi

case "$tag" in
  v*) ;;
  *) die "tag must start with 'v' so it triggers the build workflow: $tag" ;;
esac

git check-ref-format "refs/tags/$tag" || die "invalid git tag name: $tag"

if git rev-parse --verify "refs/tags/$tag" >/dev/null 2>&1; then
  die "local tag already exists: $tag"
fi

if git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1; then
  die "remote tag already exists: $tag"
fi

grep -Fq 'from faststack.app import cli' faststack/__main__.py \
  || die "faststack/__main__.py must use an absolute import for PyInstaller"
grep -Fq 'ROOT = Path(SPECPATH).parent' packaging/faststack.spec \
  || die "packaging/faststack.spec has an unexpected ROOT path"
grep -Fq "FALLBACK_VERSION = \"$version\"" faststack/updater.py \
  || die "faststack/updater.py FALLBACK_VERSION does not match pyproject version $version"
grep -Fq 'gh release create' .github/workflows/build-executables.yml \
  || die "build workflow is not configured to publish GitHub Release assets"

if [ "$skip_ruff" = false ]; then
  python_bin="$(find_venv_python)" \
    || die "no project virtualenv Python found; expected .venv or venv"
  note "Running Ruff with $python_bin"
  "$python_bin" -m ruff check faststack/
else
  note "Skipping Ruff"
fi

target_sha="$(git rev-parse "$target_ref^{commit}")"
remote_url="$(git remote get-url origin)"
repo_slug="$(printf '%s\n' "$remote_url" \
  | sed -E 's#^git@github.com:##; s#^https://github.com/##; s#\.git$##')"
repo_url="https://github.com/${repo_slug}"

note "Release tag: $tag"
note "Target ref:  $target_ref"
note "Target SHA:  $target_sha"

if [ "$dry_run" = true ]; then
  note "Dry run complete; no tag was created or pushed"
  exit 0
fi

git tag -a "$tag" "$target_sha" -m "Build FastStack $version ($tag)"
git push origin "refs/tags/$tag"

cat <<EOF

Pushed $tag.

GitHub Actions will build:
  - FastStack-windows-x64.zip
  - FastStack-macos-x64.zip
  - FastStack-macos-arm64.zip

Actions:
  ${repo_url}/actions/workflows/build-executables.yml

Release assets will appear here after the workflow completes:
  ${repo_url}/releases/tag/${tag}
EOF
