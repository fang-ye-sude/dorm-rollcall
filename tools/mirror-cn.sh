#!/bin/bash
# 把仓库镜像到国内平台（Gitee / GitCode 通用）
#
# Gitee：
#   export GITEE_TOKEN=你的私人令牌        # 设置 → 私人令牌，勾 projects
#   export GITEE_USER=你的用户名
#   ./tools/mirror-cn.sh gitee
#
# 其他平台（GitCode 等）：
#   export CN_REMOTE=https://用户名:令牌@gitcode.com/用户名/dorm-rollcall.git
#   ./tools/mirror-cn.sh custom
set -e
cd "$(dirname "$0")/.."
NAME=${REPO_NAME:-dorm-rollcall}

case "${1:-gitee}" in
  gitee)
    : "${GITEE_TOKEN:?先 export GITEE_TOKEN=<私人令牌>}"
    : "${GITEE_USER:?先 export GITEE_USER=<用户名>}"
    echo "== 在 Gitee 建仓库（已存在则忽略）"
    curl -s -X POST https://gitee.com/api/v5/user/repos \
      -d "access_token=$GITEE_TOKEN" -d "name=$NAME" -d "private=false" \
      -d "description=宿舍点名与填表自动化（脱敏开源版）" >/dev/null || true
    REMOTE="https://oauth2:$GITEE_TOKEN@gitee.com/$GITEE_USER/$NAME.git"
    URL="https://gitee.com/$GITEE_USER/$NAME"
    ;;
  custom)
    : "${CN_REMOTE:?先 export CN_REMOTE=<完整仓库地址，含令牌>}"
    REMOTE="$CN_REMOTE"
    URL="$(echo "$CN_REMOTE" | sed 's#//[^@]*@#//#')"
    ;;
  *) echo "用法: $0 [gitee|custom]"; exit 1 ;;
esac

echo "== 推送到 $URL"
git remote remove cn 2>/dev/null || true
git remote add cn "$REMOTE"
for i in 1 2 3; do
  git push -f cn main && break || { echo "重试 $i"; sleep 5; }
done
git remote set-url cn "$URL"
echo "== 完成：$URL"
echo "提示：国内平台首次公开仓库通常需要实名认证；镜像后再跑一次 tools/audit_desensitize.py 更稳妥。"
