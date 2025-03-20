from urllib import request
import json

def send_slack_block_message(blocks, webhook_url):
    if not webhook_url:
        print("Slack Webhook URL이 설정되지 않았습니다.")
        return
    
    payload = {"blocks": blocks}
    data = json.dumps(payload).encode('utf-8')
    
    try:
        req = request.Request(
            webhook_url,
            data=data,
            headers={'Content-Type': 'application/json'}
        )
        with request.urlopen(req) as response:
            if response.status != 200:
                raise Exception(f"HTTP 에러: {response.status}")
    except Exception as e:
        print(f"Slack 메시지 전송 실패: {e}")

def notify_ec2_suggestions(suggestions, webhook_url):
    if not suggestions:
        return

    blocks = []

    header_block = {
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": "🚨 EC2 Instance Optimization Suggestions",
            "emoji": True
        }
    }

    blocks.append(header_block)
    blocks.append({"type": "divider"})

    for s in suggestions:
        suggestion_text = (
            f"*Region:* `{s['Region']}`\n"
            f"*Instance ID:* `{s['InstanceId']}`\n"
            f"*Instance Name:* `{s['NameTag']}`\n"
            f"*Current Instance Type:* `{s['CurrentType']}`\n"
            f"*CPU Utilization Average:* `{s['AverageCPU']}%` (Max `{s['MaxCPU']}%`)\n"
            f"🔧 *Recommended Instance Type:* Change to `{s['SuggestedType']}`"
        )

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": suggestion_text}
        })

        # 각 suggestion마다 approve 버튼 추가
        action_value = json.dumps({
            "instance_id": s['InstanceId'],
            "current_type": s['CurrentType'],
            "suggested_type": s['SuggestedType'],
            "region": s['Region']
        })

        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Approve this change",
                        "emoji": True
                    },
                    "style": "primary",
                    "value": action_value,
                    "action_id": f"approve_ec2_change_{s['InstanceId']}"
                }
            ]
        })
        blocks.append({"type": "divider"})

    send_slack_block_message(blocks, webhook_url)