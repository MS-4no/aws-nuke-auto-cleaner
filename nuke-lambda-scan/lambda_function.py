import subprocess
import os
import shutil
import stat
import json
import requests
import re
import traceback
import boto3 # AWS操作用 リソースの利用期限すぎてるリソースに削除対象にするタグをつけるため
from datetime import datetime # 日付比較用

# --- 設定 ---
AWS_NUKE_BINARY_NAME = 'aws-nuke' 
CONFIG_FILE_NAME = 'config.yaml'

# --- ★修正: 期限切れリソース一括タグ付け関数 (Tagging API使用) ---
def check_and_tag_expired_resources(target_region):
    """
    AWS Resource Groups Tagging API を使用して、
    サービスの種類（EC2, S3, RDS...）に関係なく一括で
    ExpirationDateタグをチェックし、期限切れなら cleanup-target: true を付与する。
    """
    print("Checking for expired resources using Tagging API...")
    today = int(datetime.now().strftime('%Y%m%d')) # 例: 20251127
    
    try:
        # タグ操作用のクライアント (指定リージョンで動作)
        tagging_client = boto3.client('resourcegroupstaggingapi', region_name=target_region)

        resources_to_tag = []
        pagination_token = ""

        while True:
            # 1. ExpirationDate タグが付いているリソースを全サービスから検索
            # (1回で最大100件まで取得)
            kwargs = {
                'TagFilters': [{'Key': 'ExpirationDate'}],
                'ResourcesPerPage': 100
            }
            if pagination_token:
                kwargs['PaginationToken'] = pagination_token

            response = tagging_client.get_resources(**kwargs)

            for resource in response['ResourceTagMappingList']:
                resource_arn = resource['ResourceARN']
                tags = {t['Key']: t['Value'] for t in resource['Tags']}

                # 既に cleanup-target: true が付いていればスキップ
                if tags.get('cleanup-target') == 'true':
                    continue

                # 日付判定
                exp_date_str = tags.get('ExpirationDate')
                
                # 数字8桁かチェック
                if exp_date_str and exp_date_str.isdigit() and len(exp_date_str) == 8:
                    if int(exp_date_str) < today:
                        print(f"[Expired] Found resource: {resource_arn} (Limit: {exp_date_str})")
                        resources_to_tag.append(resource_arn)
                else:
                    # 日付フォーマット不正などはログに出してスキップ
                    print(f"[Skip] Invalid date format: {resource_arn} ({exp_date_str})")

            # 次のページがあるか確認
            pagination_token = response.get('PaginationToken', '')
            if not pagination_token:
                break

        # 2. 一括でタグ付け＆ownerタグ除去 (最大20件ずつバッチ処理)
        if resources_to_tag:
            print(f"Tagging {len(resources_to_tag)} resources...")

            # Tagging APIの制限で一度に20件までしかタグ付けできないため分割
            batch_size = 20
            for i in range(0, len(resources_to_tag), batch_size):
                batch_arns = resources_to_tag[i:i + batch_size]
                try:
                    tagging_client.tag_resources(
                        ResourceARNList=batch_arns,
                        Tags={'cleanup-target': 'true'}
                    )
                    # ownerタグを除去してaws-nukeのownerフィルター保護をバイパス
                    tagging_client.untag_resources(
                        ResourceARNList=batch_arns,
                        TagKeys=['owner']
                    )
                    print(f"Tagged batch: {len(batch_arns)} resources (owner tag removed)")
                except Exception as e:
                    print(f"Error tagging batch: {e}")
        else:
            print("No new expired resources found.")

    except Exception as e:
        print(f"Error in Tagging API: {e}")
        traceback.print_exc()


# --- Slack通知用関数 ---
def send_slack_notification(webhook_url, message_payload):
    try:
        response = requests.post(webhook_url, data=json.dumps(message_payload), headers={'Content-Type': 'application/json'}, timeout=15)
        response.raise_for_status()
        print("Slack notification sent successfully.")
    except requests.exceptions.RequestException as e:
        print(f"Error sending Slack notification: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Underlying Slack API response status: {e.response.status_code}")
            print(f"Underlying Slack API response text: {e.response.text}")

# --- Slack整形関数  ---
def format_aws_nuke_output_for_slack(nuke_stdout, nuke_stderr, return_code, is_dry_run, account_id, account_alias, target_region):
    
    status_text = "Unknown"
    status_icon = ":grey_question:"

    if return_code < 0 :
        status_icon = ":bangbang:"
        if return_code == -10: status_text = "Setup Error (File Not Found)"
        elif return_code == -11: status_text = "Setup Error (Config Not Found)"
        elif return_code == -20: status_text = "aws-nuke Timeout"
        elif return_code == -30: status_text = "Unexpected Lambda Error"
        else: status_text = "Lambda Script Error"
    elif return_code == 0 and not (nuke_stderr and nuke_stderr.strip()):
        status_icon = ":large_green_circle:"
        status_text = "Success"
    elif "UnauthorizedOperation" in nuke_stderr or "AccessDenied" in nuke_stderr:
        status_icon = ":warning:"
        status_text = "Permission Error"
    elif return_code != 0:
        status_icon = ":red_circle:"
        status_text = "Failed"
    else:
        status_icon = ":large_yellow_circle:"
        status_text = "Completed with Warnings"

    run_type = "Dry Run" if is_dry_run else "Actual Run"
    title = f"{status_icon} aws-nuke Execution: {status_text} ({run_type})"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": title, "emoji": True}
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Account ID:*\n`{account_id}`"},
                {"type": "mrkdwn", "text": f"*Account Alias:*\n`{account_alias}`"},
                {"type": "mrkdwn", "text": f"*Target Region:*\n`{target_region}`"},
                {"type": "mrkdwn", "text": f"*Return Code:*\n`{return_code}`"}
            ]
        }
    ]

    stdout_summary = "No significant output in stdout."
    if nuke_stdout:
        lines = nuke_stdout.strip().split('\n')
        scan_complete_line = next((line for line in lines if "Scan complete:" in line), None)
        action_lines = [line for line in lines if "would remove" in line or "successfully deleted" in line]
        
        simple_action_lines = []
        if action_lines:
            for line in action_lines:
                try:
                    parts = line.split(" - ")
                    action_part = parts[-1].strip()
                    
                    if len(parts) >= 4:
                        resource_type = parts[1].strip()
                        identifier = parts[2].strip()
                        display_name = ""
                        
                        name_match = re.search(r'tag:Name:\s*"([^"]*)"', line)
                        if name_match:
                            display_name = name_match.group(1)
                            simple_line = f"`{resource_type}`: `{display_name}` ({identifier}) ({action_part})"
                        else:
                            if identifier.startswith("arn:aws:ecs:"):
                                arn_parts = identifier.split('/')
                                if len(arn_parts) > 1:
                                    display_name = arn_parts[-1]
                            if not display_name:
                                display_name = identifier
                            simple_line = f"`{resource_type}`: `{display_name}` ({action_part})"
                        simple_action_lines.append(simple_line)
                    else:
                        simple_action_lines.append(line[:150] + "..." if len(line) > 150 else line)
                except Exception as e:
                    print(f"Error parsing action line: {e}")
                    simple_action_lines.append(line[:150] + "..." if len(line) > 150 else line)
        
        if scan_complete_line:
            stdout_summary = scan_complete_line
            if simple_action_lines:
                stdout_summary += "\n*Key Actions/Targets:*\n" + "\n".join(simple_action_lines[:20])
                if len(simple_action_lines) > 20:
                    stdout_summary += f"\n... and {len(simple_action_lines) - 20} more."
        elif simple_action_lines:
            stdout_summary = "*Key Actions/Targets:*\n" + "\n".join(simple_action_lines[:20])
            if len(simple_action_lines) > 20:
                stdout_summary += f"\n... and {len(simple_action_lines) - 20} more."
        elif lines:
            stdout_summary = "Last lines of stdout:\n" + "\n".join(lines[-3:])

    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*stdout Summary:*\n```\n{stdout_summary}\n```"}})

    if nuke_stderr and nuke_stderr.strip():
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*stderr:*\n```\n{nuke_stderr[:2800]}\n```"}})
    
    try:
        log_group_name = os.environ.get('AWS_LAMBDA_LOG_GROUP_NAME')
        log_stream_name = os.environ.get('AWS_LAMBDA_LOG_STREAM_NAME')
        aws_lambda_region = os.environ.get('AWS_REGION')
        if log_group_name and log_stream_name and aws_lambda_region:
            encoded_log_group = requests.utils.quote(log_group_name, safe='')
            encoded_log_stream = requests.utils.quote(log_stream_name, safe='')
            log_url = f"https://{aws_lambda_region}.console.aws.amazon.com/cloudwatch/home?region={aws_lambda_region}#logsV2:log-groups/log-group/{encoded_log_group}/log-events/{encoded_log_stream}"
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"View full execution logs: <{log_url}|CloudWatch Logs>"}]
            })
    except Exception:
        pass

    if is_dry_run and return_code >= 0 and not "Error" in nuke_stderr and not "UnauthorizedOperation" in nuke_stderr and not "AccessDenied" in nuke_stderr:
        action_value = {
            "account_id": account_id,
            "account_alias": account_alias,
            "region": target_region
        }
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "actions",
            "block_id": "nuke_approval_action",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve & Nuke (削除実行)", "emoji": True},
                    "style": "danger",
                    "value": json.dumps(action_value),
                    "action_id": "approve_nuke_button",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "本当に削除しますか？"},
                        "text": {"type": "mrkdwn", "text": f"アカウント: `{account_alias} ({account_id})`\nリージョン: `{target_region}`\n\n上記で *Dry Run* されたリソースが *本当に削除* されます。"},
                        "confirm": {"type": "plain_text", "text": "はい、削除します"},
                        "deny": {"type": "plain_text", "text": "キャンセル"}
                    }
                }
            ]
        })
    return {"blocks": blocks}

# --- lambda_handler ---
def lambda_handler(event, context):
    print(f"Lambda execution started. RequestId: {context.aws_request_id}")

    tmp_dir = "/tmp"
    lambda_task_root = os.environ.get('LAMBDA_TASK_ROOT', '.')
    aws_nuke_binary_orig_path = os.path.join(lambda_task_root, AWS_NUKE_BINARY_NAME)
    config_file_orig_path = os.path.join(lambda_task_root, CONFIG_FILE_NAME)

    aws_nuke_binary_path = os.path.join(tmp_dir, os.path.basename(AWS_NUKE_BINARY_NAME))
    config_file_path = os.path.join(tmp_dir, CONFIG_FILE_NAME)
    
    is_dry_run = True # ★★★ このLambdaは常にDry Run ★★★

    target_aws_account_id_from_config = event.get('target_account_id', "123456789012")
    account_alias_from_config = event.get('target_account_alias', "your-account-alias")
    target_region_from_config = event.get('target_region', "ALL")

    slack_webhook_url = os.environ.get('SLACK_WEBHOOK_URL')

    nuke_stdout_str = ""
    nuke_stderr_str = ""
    nuke_return_code = -1

    try:
        # ★★★ 変更点: 汎用タグ付け関数を呼び出し ★★★
        # S3もEC2もRDSも、タグが付いていればすべてここでチェックされます
        check_and_tag_expired_resources(target_region_from_config)

        # --- 実行ファイルと設定ファイルの準備 ---
        if not os.path.exists(aws_nuke_binary_orig_path):
            nuke_return_code = -10
            raise FileNotFoundError(f"aws-nuke binary '{AWS_NUKE_BINARY_NAME}' not found.")
        
        if not os.path.exists(config_file_orig_path):
            nuke_return_code = -11
            raise FileNotFoundError(f"aws-nuke config file '{CONFIG_FILE_NAME}' not found.")

        shutil.copyfile(aws_nuke_binary_orig_path, aws_nuke_binary_path)
        shutil.copyfile(config_file_orig_path, config_file_path)
        
        os.chmod(aws_nuke_binary_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        # --- aws-nuke 実行コマンド ---
        command = [
            aws_nuke_binary_path,
            "run", #新しいaws-nukeで必要なので追加
            "-c", config_file_path,
            "--force",
            "--force-sleep", "5"
        ]
        
        print(f"Executing command: {' '.join(command)}")
        
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_dir)
        timeout_seconds = (context.get_remaining_time_in_millis() / 1000) * 0.9
        
        stdout_bytes, stderr_bytes = process.communicate(timeout=timeout_seconds)
        
        nuke_stdout_str = stdout_bytes.decode(errors='replace')
        nuke_stderr_str = stderr_bytes.decode(errors='replace')
        nuke_return_code = process.returncode

        print("--- aws-nuke STDOUT ---")
        print(nuke_stdout_str)
        print("--- aws-nuke STDERR ---")
        if nuke_stderr_str: print(nuke_stderr_str)
        print(f"--- aws-nuke Return Code: {nuke_return_code} ---")

    except Exception as e:
        if nuke_return_code == -1: nuke_return_code = -30
        nuke_stderr_str = f"An error occurred (Dry Run): {str(e)}\n{traceback.format_exc()}"
        print(nuke_stderr_str)
    
    # --- Slack通知 ---
    if slack_webhook_url:
        message_payload = format_aws_nuke_output_for_slack(
            nuke_stdout_str, nuke_stderr_str, nuke_return_code, is_dry_run, 
            target_aws_account_id_from_config,
            account_alias_from_config,
            target_region_from_config
        )
        send_slack_notification(slack_webhook_url, message_payload)
    
    response_body = {
        'stdout': nuke_stdout_str,
        'stderr': nuke_stderr_str,
        'return_code': nuke_return_code
    }

    return {
        'statusCode': 200, 
        'body': json.dumps(response_body)
    }