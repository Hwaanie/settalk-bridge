#!/usr/bin/env bash
# 이미 만들어 둔 GitHub 저장소(예: Hwaanie/settalk-bridge)로 이 폴더를 올린다.
#
#   1) github.com/new 에서 settalk-bridge (Public) 저장소를 만든 뒤
#   2) Pickle 앱의 GitHub 연결 설정에서 그 저장소 접근을 열어 준 뒤
#   3) 아래처럼 실행:
#
#      bash push.sh Hwaanie/settalk-bridge
#
# 이미 push된 저장소를 다시 올릴 때도 그대로 쓰면 된다(변경분만 올라감).
set -euo pipefail

REPO="${1:?사용법: bash push.sh <owner>/<repo>}"
cd "$(dirname "$0")"

if [ ! -d .git ]; then
  git init -q
  git symbolic-ref HEAD refs/heads/main
fi

git add -A
if ! git diff --cached --quiet; then
  git -c user.name="${GIT_AUTHOR_NAME:-Hwaanie}" \
      -c user.email="${GIT_AUTHOR_EMAIL:-Hwaanie@users.noreply.github.com}" \
      commit -q -m "Add Settalk mention-only bridge (10-minute polling, replies only on @mention)"
fi

git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/${REPO}.git"
git push -u origin main
echo "완료: https://github.com/${REPO}"
