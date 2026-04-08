# AWS公式のLambdaベースイメージを使用
FROM public.ecr.aws/lambda/python:3.12

# 必要なライブラリをインストール
RUN pip install requests boto3

# ファイル一式を /var/task にコピー
COPY . /var/task/

# --- 追加箇所 ---
# すべてのファイルに読み取り権限、ディレクトリに実行権限を付与（Lambdaユーザー用）
RUN chmod -R 755 /var/task
# --- ここまで ---

# aws-nuke バイナリをダウンロードして配置
RUN dnf install -y tar gzip \
    && curl -L https://github.com/ekristen/aws-nuke/releases/download/v3.64.1/aws-nuke-v3.64.1-linux-amd64.tar.gz -o /tmp/aws-nuke.tar.gz \
    && tar xzf /tmp/aws-nuke.tar.gz -C /var/task/ aws-nuke \
    && chmod +x /var/task/aws-nuke \
    && rm /tmp/aws-nuke.tar.gz \
    && dnf clean all

# Lambdaハンドラを指定
CMD [ "lambda_function.lambda_handler" ]