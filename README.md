# aws-nuke-auto-cleaner

AWS アカウント内の不要リソースを自動検出・削除するシステム。
aws-nuke を Lambda 上で実行し、結果を Slack に通知する。

## 構成

```
nuke-lambda-scan/    ... Dry Run（スキャン）用 Lambda
nuke-lambda-delete/  ... Actual Run（削除実行）用 Lambda
```

## 全体の流れ

```
1. [EventBridge等] scan Lambda を定期実行
2. [scan Lambda]
   a. ExpirationDate タグをチェック → 期限切れリソースの owner タグを除去
   b. aws-nuke を Dry Run で実行
   c. 結果を Slack に通知（削除対象リソース一覧 + "Approve & Nuke" ボタン）
3. [Slack] ユーザーが "Approve & Nuke" ボタンを押下
4. [API Gateway → delete Lambda] aws-nuke を Actual Run（--no-dry-run）で実行
5. [delete Lambda] 削除結果を Slack に通知
```

## 削除判定ロジック

リソースが削除対象になる条件（いずれかに該当）:

| 条件 | 説明 |
|------|------|
| owner タグなし | owner タグが付いていないリソースは削除対象 |
| 期限切れ | `ExpirationDate` タグの値（YYYYMMDD）が今日より前の場合、scan Lambda が owner タグを除去 → 削除対象になる |

リソースが保護される条件（いずれかに該当）:

| 条件 | 説明 |
|------|------|
| owner タグあり | `tag:owner` が既知のオーナーリストに一致するリソースは保護 |

### 注意事項

- aws-nuke のフィルターは OR 条件（いずれかにマッチすれば保護）
- タグキーは **大文字小文字を区別する**
- AWS Tagging API はタグが 0 個のリソースを返せないため、タグなしリソースの判定は config.yaml のフィルターで行う

## config.yaml

### resource-types.includes

スキャン対象のリソースタイプを指定:

- コンピューティング: EC2Instance, EKSCluster, ECSCluster, LambdaFunction 等
- ストレージ: S3Bucket, EBSVolume
- データベース: RDSInstance, DynamoDBTable 等
- ネットワーク: NATGateway, ElasticIP, VPCEndpoint
- その他: CloudFrontDistribution, EMRCluster 等

### accounts.filters.__global__

全リソースタイプに適用されるフィルター（保護ルール）:

- `tag:owner` = 既知のオーナー名 → 保護

## scan Lambda (lambda_function.py)

### check_and_tag_expired_resources()

1. Tagging API で `ExpirationDate` タグ付きリソースを全サービスから検索
2. `ExpirationDate` の値（YYYYMMDD）が今日より前なら:
   - `cleanup-target: true` タグを付与
   - `owner` タグを除去（aws-nuke の owner フィルター保護をバイパス）
3. 既に `cleanup-target: true` が付いているリソースはスキップ

### lambda_handler()

1. `check_and_tag_expired_resources()` を実行
2. aws-nuke を Dry Run モードで実行
3. 結果を Slack Webhook で通知

## delete Lambda (lambda_function.py)

1. Slack の "Approve & Nuke" ボタンから API Gateway 経由で呼び出される
2. aws-nuke を `--no-dry-run` で実行（実際に削除）
3. 削除結果を Slack の `response_url` で通知

## デプロイ手順

scan / delete それぞれのディレクトリで同じ手順を実行する。

```bash
# --- scan Lambda ---
cd nuke-lambda-scan

docker build --platform linux/amd64 -t nuke-lambda-scan .
docker tag nuke-lambda-scan:latest 123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/nuke-lambda-scan:latest
docker push 123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/nuke-lambda-scan:latest

# --- delete Lambda ---
cd nuke-lambda-delete

docker build --platform linux/amd64 -t nuke-lambda-delete .
docker tag nuke-lambda-delete:latest 123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/nuke-lambda-delete:latest
docker push 123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/nuke-lambda-delete:latest
```

※ Lambda コンソールで新しいイメージを選択してデプロイ

## 環境変数（Lambda）

| 変数名 | 用途 | 対象 |
|--------|------|------|
| `SLACK_WEBHOOK_URL` | Slack 通知用 Webhook URL | scan |

## オーナーリスト

config.yaml の `tag:owner` フィルターに登録されているオーナー:

member-a, member-b, member-c, ...

