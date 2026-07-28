#!/usr/bin/env bash
# One-shot release helper for portrait-gallery.
# Bumps VERSION/docs, commits + tags, pushes, builds multi-arch Docker image,
# and creates the matching GitHub Release.
#
# Examples:
#   ./scripts/release.sh 1.3.9 --notes-file notes.md
#   ./scripts/release.sh --bump patch --notes $'- fix A\n- feat B'
#   ./scripts/release.sh 1.3.9 --dry-run
#   ./scripts/release.sh --docker-only
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IMAGE_REPO="${IMAGE_REPO:-ikirito9/hermes-portrait-gallery}"
BUILDER="${BUILDX_BUILDER:-portrait-gallery-publisher}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"
GH_REPO="${GH_REPO:-i-kirito/portrait-gallery}"

VERSION_ARG=""
BUMP=""
NOTES=""
NOTES_FILE=""
DRY_RUN=0
SKIP_TESTS=0
SKIP_DOCKER=0
SKIP_GITHUB=0
SKIP_PUSH=0
DOCKER_ONLY=0
ALLOW_DIRTY=0
YES=0
NO_COMMIT=0

usage() {
  cat <<'EOF'
Usage:
  ./scripts/release.sh <version> [options]
  ./scripts/release.sh --bump patch|minor|major [options]

Options:
  --notes TEXT              Release note bullets
  --notes-file PATH         Read release notes from file
  --bump patch|minor|major  Auto-bump from current VERSION
  --image REPO              Docker image repo
  --builder NAME            buildx builder name
  --platforms LIST          buildx platforms
  --remote NAME             git remote
  --branch NAME             release branch
  --repo OWNER/NAME         GitHub repo
  --dry-run                 Print actions only
  --yes                     Skip confirmation
  --allow-dirty             Allow dirty worktree
  --skip-tests              Skip compileall/unittest
  --skip-docker             Skip Docker build/push
  --skip-github             Skip GitHub Release
  --skip-push               Skip git/docker/gh publish side effects
  --docker-only             Only publish image for current VERSION
  --no-commit               Update local files only
  -h, --help                Show help
EOF
}

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

is_semver() {
  [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]
}

normalize_version() {
  local raw="$1"
  raw="${raw#v}"
  printf '%s' "$raw"
}

version_tag() {
  printf 'v%s' "$1"
}

bump_version() {
  local current="$1" kind="$2"
  local major minor patch
  IFS=. read -r major minor patch <<<"$current"
  patch="${patch%%[!0-9]*}"
  case "$kind" in
    patch) patch=$((patch + 1)) ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    major) major=$((major + 1)); minor=0; patch=0 ;;
    *) die "unknown bump kind: $kind" ;;
  esac
  printf '%s.%s.%s' "$major" "$minor" "$patch"
}

current_version() {
  tr -d '[:space:]' < VERSION
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help) usage; exit 0 ;;
      --notes) NOTES="${2:-}"; shift 2 ;;
      --notes-file) NOTES_FILE="${2:-}"; shift 2 ;;
      --bump) BUMP="${2:-}"; shift 2 ;;
      --image) IMAGE_REPO="${2:-}"; shift 2 ;;
      --builder) BUILDER="${2:-}"; shift 2 ;;
      --platforms) PLATFORMS="${2:-}"; shift 2 ;;
      --remote) REMOTE="${2:-}"; shift 2 ;;
      --branch) BRANCH="${2:-}"; shift 2 ;;
      --repo) GH_REPO="${2:-}"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      --yes|-y) YES=1; shift ;;
      --allow-dirty) ALLOW_DIRTY=1; shift ;;
      --skip-tests) SKIP_TESTS=1; shift ;;
      --skip-docker) SKIP_DOCKER=1; shift ;;
      --skip-github) SKIP_GITHUB=1; shift ;;
      --skip-push) SKIP_PUSH=1; shift ;;
      --docker-only) DOCKER_ONLY=1; shift ;;
      --no-commit) NO_COMMIT=1; shift ;;
      --) shift; break ;;
      -*) die "unknown option: $1" ;;
      *)
        if [[ -n "$VERSION_ARG" ]]; then
          die "unexpected extra argument: $1"
        fi
        VERSION_ARG="$1"
        shift
        ;;
    esac
  done
}

confirm() {
  local prompt="$1" answer
  if [[ "$YES" -eq 1 || "$DRY_RUN" -eq 1 ]]; then
    return 0
  fi
  read -r -p "$prompt [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" || "$answer" == "yes" ]]
}

ensure_clean_worktree() {
  local dirty
  dirty="$(git status --porcelain)"
  if [[ -z "$dirty" ]]; then
    return 0
  fi
  if [[ "$ALLOW_DIRTY" -eq 1 ]]; then
    warn "worktree is dirty; continuing because --allow-dirty was set"
    printf '%s\n' "$dirty"
    return 0
  fi
  printf 'error: worktree is dirty; commit/stash first, or pass --allow-dirty\n%s\n' "$dirty" >&2
  exit 1
}

ensure_on_branch() {
  local current
  current="$(git rev-parse --abbrev-ref HEAD)"
  if [[ "$current" != "$BRANCH" ]]; then
    die "current branch is '$current', expected '$BRANCH'"
  fi
}

auto_notes_from_git() {
  local prev
  prev="$(git describe --tags --abbrev=0 2>/dev/null || true)"
  if [[ -z "$prev" ]]; then
    git log -5 --pretty=format:'- %s' --no-merges
    printf '\n'
    return 0
  fi
  git log "${prev}..HEAD" --pretty=format:'- %s' --no-merges
  printf '\n'
}

read_notes() {
  if [[ -n "$NOTES_FILE" ]]; then
    [[ -f "$NOTES_FILE" ]] || die "notes file not found: $NOTES_FILE"
    NOTES="$(cat "$NOTES_FILE")"
  fi
  if [[ -z "${NOTES//[[:space:]]/}" ]]; then
    NOTES="$(auto_notes_from_git)"
  fi
  NOTES="$(printf '%s\n' "$NOTES" | sed -E 's/\r$//')"
  if ! printf '%s\n' "$NOTES" | grep -qE '^[[:space:]]*([-*]|[0-9]+\.)[[:space:]]'; then
    NOTES="$(printf '%s\n' "$NOTES" | sed -E '/^[[:space:]]*$/d; s/^[[:space:]]*/- /')"
  fi
  [[ -n "${NOTES//[[:space:]]/}" ]] || die "release notes are empty; pass --notes or --notes-file"
}

extract_readme_notes() {
  local version="$1"
  python3 - "$version" <<'PY'
import re
import sys
from pathlib import Path

version = sys.argv[1]
text = Path("README.md").read_text(encoding="utf-8")
pattern = rf"(?ms)^### v{re.escape(version)}\s*\n(.*?)(?=^### v|\Z)"
match = re.search(pattern, text)
if not match:
    raise SystemExit(f"README notes for v{version} not found")
print(match.group(1).strip())
PY
}

update_version_files() {
  local version="$1"
  local notes="$2"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "would update VERSION, README.md, AGENTS.md and SKILL.md for $version"
    return 0
  fi
  python3 - "$version" "$notes" <<'PY'
import pathlib
import re
import sys

version = sys.argv[1]
notes = sys.argv[2].rstrip() + "\n"
root = pathlib.Path(".")
(root / "VERSION").write_text(version + "\n", encoding="utf-8")


def replace_version_pins(text: str) -> str:
    text = re.sub(
        r"(当前版本：\*\*)v?\d+\.\d+\.\d+(\*\*)",
        rf"\g<1>v{version}\2",
        text,
        count=1,
    )
    text = re.sub(
        r"(hermes-portrait-gallery:)\d+\.\d+\.\d+",
        rf"\g<1>{version}",
        text,
    )
    text = re.sub(
        r"(PORTRAIT_GALLERY_IMAGE=REGISTRY_OR_USER/hermes-portrait-gallery:)\d+\.\d+\.\d+",
        rf"\g<1>{version}",
        text,
    )
    return text


def upsert_release_notes(text: str, version: str, notes: str) -> str:
    match = re.search(r"(?m)^## .*Release Notes\s*$", text)
    if not match:
        raise SystemExit("Release Notes section missing in README.md")
    start = match.start()
    end = match.end()
    while end < len(text) and text[end] == "\n":
        end += 1
        break
    if end < len(text) and text[end] == "\n":
        end += 1
    marker = text[start:end]
    head = text[:start]
    rest = text[end:]
    block = f"### v{version}\n\n{notes.rstrip()}\n\n"
    same = re.compile(rf"(?ms)^### v{re.escape(version)}\s*\n.*?(?=^### v|\Z)")
    if rest.lstrip().startswith(f"### v{version}"):
        return head + marker + same.sub(block, rest, count=1)
    return head + marker + block + rest


for rel in ("README.md", "AGENTS.md", "SKILL.md"):
    path = root / rel
    if not path.exists():
        continue
    original = path.read_text(encoding="utf-8")
    updated = replace_version_pins(original)
    if rel == "README.md":
        updated = upsert_release_notes(updated, version, notes)
    if updated != original:
        path.write_text(updated, encoding="utf-8")
        print(f"updated {rel}")
    else:
        print(f"unchanged {rel}")

print(f"VERSION -> {version}")
PY
}

run_tests() {
  if [[ "$SKIP_TESTS" -eq 1 ]]; then
    warn "skipping tests"
    return 0
  fi
  local py
  if [[ -x .venv/bin/python ]]; then
    py=".venv/bin/python"
  else
    py="python3"
  fi
  log "compileall"
  run "$py" -m compileall app
  log "unit tests"
  run "$py" -m unittest discover -s tests
}

git_commit_tag_push() {
  local version="$1"
  local tag msg
  tag="$(version_tag "$version")"

  run git add VERSION README.md AGENTS.md SKILL.md
  if [[ "$DRY_RUN" -eq 0 ]]; then
    if git diff --cached --quiet; then
      die "nothing staged for release commit; VERSION/docs already match $version?"
    fi
  fi

  msg="chore: release ${version}

Ship ${tag}."
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run] git commit -m %q\n' "$msg"
  else
    git commit -m "$msg"
  fi

  if git rev-parse "$tag" >/dev/null 2>&1; then
    die "tag already exists: $tag"
  fi
  run git tag -a "$tag" -m "$tag"

  if [[ "$SKIP_PUSH" -eq 1 ]]; then
    warn "skipping git push"
    return 0
  fi
  run git push "$REMOTE" "$BRANCH"
  run git push "$REMOTE" "$tag"
}

ensure_builder() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "would ensure buildx builder: $BUILDER"
    return 0
  fi
  if docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    docker buildx start "$BUILDER" >/dev/null || true
    return 0
  fi
  warn "builder '$BUILDER' missing; creating docker-container builder"
  docker buildx create --name "$BUILDER" --driver docker-container --use --bootstrap
}

docker_publish() {
  local version="$1"
  local version_tag latest_tag
  version_tag="${IMAGE_REPO}:${version}"
  latest_tag="${IMAGE_REPO}:latest"

  need_cmd docker
  ensure_builder

  if [[ "$SKIP_PUSH" -eq 1 && "$DRY_RUN" -eq 0 ]]; then
    warn "skipping docker push; planned tags only"
    log "would build/push: $version_tag and $latest_tag ($PLATFORMS) via $BUILDER"
    return 0
  fi

  log "docker buildx build/push $version_tag + $latest_tag ($PLATFORMS)"
  run docker buildx build \
    --builder "$BUILDER" \
    --platform "$PLATFORMS" \
    -t "$version_tag" \
    -t "$latest_tag" \
    -f Dockerfile \
    --push \
    .
}

github_release() {
  local version="$1"
  local tag notes body
  tag="$(version_tag "$version")"
  need_cmd gh

  if [[ "$DRY_RUN" -eq 1 ]]; then
    notes="$NOTES"
  else
    notes="$(extract_readme_notes "$version")"
  fi

  body="$(python3 - "$tag" "$notes" "$IMAGE_REPO" "$version" <<'PY2'
import sys
tag, notes, image_repo, version = sys.argv[1:5]
print(f"""## {tag}

{notes}

### Docker

```bash
docker pull {image_repo}:{version}
```

支持 `linux/amd64` 与 `linux/arm64`。""")
PY2
)"

  if [[ "$SKIP_PUSH" -eq 1 ]]; then
    warn "skipping GitHub release publish"
    printf '%s\n' "$body"
    return 0
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    run gh release create "$tag" --repo "$GH_REPO" --title "$tag" --notes "$body"
  elif gh release view "$tag" --repo "$GH_REPO" >/dev/null 2>&1; then
    warn "GitHub release $tag already exists; updating notes"
    run gh release edit "$tag" --repo "$GH_REPO" --title "$tag" --notes "$body"
  else
    run gh release create "$tag" --repo "$GH_REPO" --title "$tag" --notes "$body"
  fi
}


print_summary() {
  local version="$1"
  local tests_state docker_state github_state
  if [[ "$SKIP_TESTS" -eq 1 ]]; then tests_state=skip; else tests_state=run; fi
  if [[ "$SKIP_DOCKER" -eq 1 ]]; then docker_state=skip; else docker_state=publish; fi
  if [[ "$SKIP_GITHUB" -eq 1 ]]; then github_state=skip; else github_state=publish; fi
  printf '\nRelease plan\n'
  printf '  version : %s (%s)\n' "$version" "$(version_tag "$version")"
  printf '  branch  : %s\n' "$BRANCH"
  printf '  remote  : %s\n' "$REMOTE"
  printf '  image   : %s:%s + :latest\n' "$IMAGE_REPO" "$version"
  printf '  builder : %s\n' "$BUILDER"
  printf '  plats   : %s\n' "$PLATFORMS"
  printf '  github  : %s\n' "$GH_REPO"
  printf '  dry-run : %s\n' "$DRY_RUN"
  printf '  tests   : %s\n' "$tests_state"
  printf '  docker  : %s\n' "$docker_state"
  printf '  github  : %s\n' "$github_state"
}

main() {
  parse_args "$@"
  need_cmd git
  need_cmd python3

  local version current
  if [[ "$DOCKER_ONLY" -eq 1 ]]; then
    version="$(current_version)"
    is_semver "$version" || die "invalid VERSION file: $version"
    print_summary "$version"
    confirm "Publish Docker image for $version now?" || die "aborted"
    docker_publish "$version"
    log "done (docker-only)"
    return 0
  fi

  if [[ -n "$VERSION_ARG" && -n "$BUMP" ]]; then
    die "pass either an explicit version or --bump, not both"
  fi
  if [[ -n "$VERSION_ARG" ]]; then
    version="$(normalize_version "$VERSION_ARG")"
  elif [[ -n "$BUMP" ]]; then
    version="$(bump_version "$(current_version)" "$BUMP")"
  else
    die "version required (e.g. 1.3.9) or use --bump patch|minor|major"
  fi
  is_semver "$version" || die "invalid version: $version"

  current="$(current_version)"
  if [[ "$version" == "$current" && "$NO_COMMIT" -eq 0 ]]; then
    warn "target version equals current VERSION ($current)"
  fi

  ensure_on_branch
  ensure_clean_worktree
  read_notes
  print_summary "$version"
  printf '\nNotes:\n%s\n' "$NOTES"
  confirm "Proceed with release $version?" || die "aborted"

  update_version_files "$version" "$NOTES"
  run_tests

  if [[ "$NO_COMMIT" -eq 1 ]]; then
    log "updated local release files only (--no-commit)"
    return 0
  fi

  git_commit_tag_push "$version"

  if [[ "$SKIP_DOCKER" -eq 0 ]]; then
    docker_publish "$version"
  else
    warn "skipping docker publish"
  fi

  if [[ "$SKIP_GITHUB" -eq 0 ]]; then
    github_release "$version"
  else
    warn "skipping GitHub release"
  fi

  printf '\nDone.\n'
  printf '  tag    : %s\n' "$(version_tag "$version")"
  printf '  image  : %s:%s\n' "$IMAGE_REPO" "$version"
  printf '  latest : %s:latest\n' "$IMAGE_REPO"
  printf '  pull   : docker pull %s:%s\n' "$IMAGE_REPO" "$version"
  if [[ "$SKIP_GITHUB" -eq 0 && "$SKIP_PUSH" -eq 0 && "$DRY_RUN" -eq 0 ]]; then
    gh release view "$(version_tag "$version")" --repo "$GH_REPO" --json url --jq .url || true
  fi
}

main "$@"
