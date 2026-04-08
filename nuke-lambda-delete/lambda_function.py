import subprocess
import os
import shutil
import stat
import json
import requests
import urllib.parse
import traceback
import base64
import re
import boto3

# --- 設定 ---
# Dockerイメージ内のパスに合わせて設定
AWS_NUKE_BINARY_NAME = 'aws-nuke'
CONFIG_FILE_NAME = 'config.yaml'

def send_slack_response(response_url, message_payload):
    """Slackのresponse_urlを使用してメッセージを返す関数"""
    try:
        response = requests.post(response_url, json=message_payload, timeout=15)
        response.raise_for_status()
        print("Slack response sent successfully.")
    except Exception as e:
        print(f"Error sending Slack response: {e}")

def format_aws_nuke_output_for_slack(nuke_stdout, nuke_stderr, return_code, is_dry_run, account_id, account_alias, target_region):
    """
    削除実行時の詳細なリソース名を抽出して整形する関数
    """
    status_icon = ":large_green_circle:" if return_code == 0 else ":red_circle:"
    status_text = "Success" if return_code == 0 else "Failed/Error"
    
    if nuke_stderr and ("UnauthorizedOperation" in nuke_stderr or "AccessDenied" in nuke_stderr):
        status_icon, status_text = ":warning:", "Permission Error"

    title = f"{status_icon} aws-nuke Execution: {status_text} (Actual Run)"
    
    # --- リソース名の抽出ロジック ---
    resource_lines = []
    summary_stat = ""
    
    if nuke_stdout:
        lines = nuke_stdout.strip().split('\n')
        for line in lines:
            if "Nuke complete:" in line:
                summary_stat = line.strip()
            
            # 削除成功リソースの解析
            if " - removed" in line or "successfully deleted" in line:
                try:
                    parts = line.split(" - ")
                    if len(parts) >= 3:
                        res_type = parts[1].strip()
                        res_id = parts[2].strip()
                        # tag:Name: "xxx" を抽出
                        name_match = re.search(r'tag:Name:\s*"([^"]*)"', line)
                        display_name = name_match.group(1) if name_match else res_id
                        resource_lines.append(f"• `{res_type}`: *{display_name}* (`{res_id}`)")
                except Exception:
                    continue

    stdout_summary = "\n".join(resource_lines[:20]) if resource_lines else "No resources were removed."
    if len(resource_lines) > 20:
        stdout_summary += f"\n... and {len(resource_lines) - 20} more items."

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Account:* `{account_alias} ({account_id})`"},
            {"type": "mrkdwn", "text": f"*Region:* `{target_region}`"},
            {"type": "mrkdwn", "text": f"*Status:* {summary_stat if summary_stat else 'Finished'}"}
        ]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Deleted Resources:*\n{stdout_summary}"}}
    ]
    
    if nuke_stderr:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*stderr:*\n```\n{nuke_stderr[:1000]}\n```"}})
    
    return {"blocks": blocks}

def prepare_nuke_environment(tmp_dir):
    """バイナリと設定ファイルを /tmp に配置し、実行権限を付与"""
    src_bin = os.path.join(os.environ.get('LAMBDA_TASK_ROOT', '/var/task'), AWS_NUKE_BINARY_NAME)
    src_conf = os.path.join(os.environ.get('LAMBDA_TASK_ROOT', '/var/task'), CONFIG_FILE_NAME)
    dst_bin = os.path.join(tmp_dir, AWS_NUKE_BINARY_NAME)
    dst_conf = os.path.join(tmp_dir, CONFIG_FILE_NAME)

    if not os.path.exists(src_bin):
        raise FileNotFoundError(f"Binary not found at {src_bin}")

    shutil.copyfile(src_bin, dst_bin)
    shutil.copyfile(src_conf, dst_conf)
    # 実行権限 (755) を付与
    os.chmod(dst_bin, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    return dst_bin, dst_conf

def execute_aws_nuke(config_file_path, aws_nuke_binary_path, context, is_dry_run):
    """aws-nukeを実行する。変数名 aws_nuke_binary_path を使用。"""
    command = [
        aws_nuke_binary_path,
        "run", # 最新版で必要なサブコマンド
        "-c", config_file_path,
        "--force",
        "--force-sleep", "3"
    ]
    
    if not is_dry_run:
        command.append("--no-dry-run")
    
    print(f"Executing command: {' '.join(command)}")
    
    process = subprocess.Popen(
        command, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.PIPE, 
        cwd=os.path.dirname(aws_nuke_binary_path)
    )
    
    # タイムアウト対策 (Lambdaの残り時間から10秒引いた時間を上限にする)
    timeout = (context.get_remaining_time_in_millis() / 1000) - 10
    stdout, stderr = process.communicate(timeout=timeout)
    
    return stdout.decode(errors='replace'), stderr.decode(errors='replace'), process.returncode

def lambda_handler(event, context):
    print("Starting Actual Run execution via API Gateway...")
    is_dry_run = False 
    tmp_dir = "/tmp"

    try:
        # 1. API Gatewayからのボディ取得とBase64デコード
        body_str = event.get('body', '')
        if event.get('isBase64Encoded'):
            body_str = base64.b64decode(body_str).decode('utf-8')

        if not body_str:
            raise ValueError("Empty request body - check API Gateway Integration settings")

        # 2. Slackペイロードのパース
        parsed_qs = urllib.parse.parse_qs(body_str)
        if 'payload' not in parsed_qs:
            raise ValueError("Payload not found in request")
            
        slack_payload = json.loads(parsed_qs['payload'][0])
        action_val = json.loads(slack_payload['actions'][0]['value'])
        acc_id = action_val.get('account_id')
        alias = action_val.get('account_alias')
        region = action_val.get('region')
        response_url = slack_payload['response_url']

        # 3. Slackへの即時応答 (3秒タイムアウト回避)
        send_slack_response(response_url, {
            "text": f":wastebasket: `{alias}` の削除処理（Actual Run）を開始しました...",
            "replace_original": False
        })

        # 4. 実行環境の準備
        aws_nuke_binary_path, conf_path = prepare_nuke_environment(tmp_dir)

        # 5. aws-nuke 実行
        stdout, stderr, code = execute_aws_nuke(conf_path, aws_nuke_binary_path, context, is_dry_run)

        # 6. 結果通知
        res_payload = format_aws_nuke_output_for_slack(stdout, stderr, code, is_dry_run, acc_id, alias, region)
        send_slack_response(response_url, res_payload)

        return {'statusCode': 200, 'body': json.dumps({'status': 'ok'})}

    except Exception as e:
        print(traceback.format_exc())
        return {'statusCode': 400, 'body': json.dumps({'error': str(e)})}