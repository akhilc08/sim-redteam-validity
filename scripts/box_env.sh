# Source on the EC2 box before running ct. No secrets: models go through Bedrock via the instance role.
export AWS_DEFAULT_REGION=us-east-1
export SKIP_DOCKER_BUILD=1
export DISABLE_TUI=1
for e in AUTO_WORKFLOW CLINICAL_TRIAL FILE_INDEXING MEDIA_PROCESSING MODEL_REGISTRY \
         SHARED_DOTFILES SLACK_CLONE TIKTOK USER_TRACKING WEB_SCRAPING; do
  export "ENV_IMAGE_TAG_${e}=v1.0.1"
done
export CT="$HOME/work/linuxarena-paper/.venv/bin/ct"
# API keys (never printed): ANTHROPIC_API_KEY etc. live in ~/work/.env, chmod 600.
if [ -f "$HOME/work/.env" ]; then set -a; . "$HOME/work/.env"; set +a; fi
