import boto3, os
from datetime import datetime, timedelta, timezone
from notify import send_slack_block_message, notify_ec2_suggestions
from urllib.parse import unquote
import json
import base64
import re

# 환경설정: 임계값과 모니터링 기간 (기본값: 50%, 60분)
CPU_THRESHOLD = float(os.getenv('CPU_THRESHOLD', '50'))      # CPU 사용률 임계치(%)
MONITOR_DURATION = int(os.getenv('MONITOR_DURATION', '60'))  # 모니터링 시간(분)

# Slack 알림 설정: Slack Webhook URL (환경변수로 설정)
SLACK_WEBHOOK_URL = os.getenv('SLACK_WEBHOOK_URL')

def get_all_regions():
    # 모든 AWS 리전 목록 가져오기
    ec2 = boto3.client('ec2', region_name='us-east-1')
    regions = [region['RegionName'] for region in ec2.describe_regions()['Regions']]
    return regions

def get_running_instances(ec2_client, region):
    # 실행 중(Running)인 모든 EC2 인스턴스 조회
    instances = []
    paginator = ec2_client.get_paginator('describe_instances')
    # 현재 시간 기준 1시간 전 시간 계산
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    
    for page in paginator.paginate(Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]):
        for reservation in page['Reservations']:
            for inst in reservation['Instances']:
                # 인스턴스가 1시간 이상 실행 중인 경우만 추가
                if inst['LaunchTime'] <= one_hour_ago:
                    instances.append(inst)
    return instances

def get_cpu_utilization(cw_client, instance_id, minutes):
    # 지정한 기간 (minutes) 동안의 평균 및 최대 CPU 사용률을 반환
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=minutes)
    response = cw_client.get_metric_statistics(
        Namespace='AWS/EC2',
        MetricName='CPUUtilization',
        Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
        StartTime=start_time,
        EndTime=end_time,
        Period=60,  # 1분 간격 데이터
        Statistics=['Average', 'Maximum']
    )
    data_points = response.get('Datapoints', [])
    if not data_points:
        return 0.0, 0.0  # 데이터가 없는 경우 (인스턴스 중지 등) 0으로 처리
    # 시간순으로 정렬 후 평균과 최대 계산
    data_points.sort(key=lambda x: x['Timestamp'])
    avg_cpu = sum(dp['Average'] for dp in data_points) / len(data_points)
    max_cpu = max(dp['Maximum'] for dp in data_points)
    return avg_cpu, max_cpu

SIZE_ORDER = ["nano", "micro", "small", "medium", "large", 
              "xlarge", "2xlarge", "4xlarge", "8xlarge", 
              "12xlarge", "16xlarge", "24xlarge", "32xlarge", "48xlarge", "64xlarge"]

def suggest_instance_type(ec2_client, current_type, avg_cpu):
    # 현재 인스턴스 타입(current_type)과 평균 CPU로 크기 변경 제안 반환
    if '.' not in current_type:
        return None  # 예상치 못한 포맷의 타입 이름
    family, size = current_type.rsplit('.', 1)  # 예: "t3.medium" -> family="t3", size="medium"
    if size not in SIZE_ORDER:
        return None  # 목록에 없는 사이즈라면 처리 불가
    
    idx = SIZE_ORDER.index(size)
    lower_threshold = CPU_THRESHOLD / 2.0  # 하한 임계값 (CPU_THRESHOLD가 50일 때 25)
    upper_threshold = CPU_THRESHOLD * 1.5  # 상한 임계값 (CPU_THRESHOLD가 50일 때 75)
    
    if avg_cpu >= upper_threshold:
        # CPU 사용률이 상한 임계값 이상 -> 한 단계 큰 타입으로 제안
        if idx < len(SIZE_ORDER) - 1:
            try:
                if ec2_client.describe_instance_types(InstanceTypes=[f"{family}.{SIZE_ORDER[idx+1]}"]):
                    return f"{family}.{SIZE_ORDER[idx+1]}"
            except Exception as e:
                print(f"인스턴스 타입 존재 확인 실패: {e}")
    elif avg_cpu < lower_threshold:
        # CPU 사용률이 하한 임계값 미만 -> 한 단계 작은 타입으로 제안
        if idx > 0:
            try:
                if ec2_client.describe_instance_types(InstanceTypes=[f"{family}.{SIZE_ORDER[idx-1]}"]):
                    return f"{family}.{SIZE_ORDER[idx-1]}"
            except Exception as e:
                print(f"인스턴스 타입 존재 확인 실패: {e}")
    
    return None  # 변경 불필요 (적정 범위 내) 또는 제안 없음

def apply_single_ec2_change(instance_data):
    """단일 EC2 인스턴스에 대한 변경을 적용합니다."""
    inst_id = instance_data['instance_id']
    new_type = instance_data['suggested_type']
    region = instance_data['region']
    
    ec2_client = boto3.client('ec2', region_name=region)
    try:
        # 1. 인스턴스 중지
        ec2_client.stop_instances(InstanceIds=[inst_id])
        ec2_client.get_waiter('instance_stopped').wait(InstanceIds=[inst_id])
        # 2. 인스턴스 타입 변경
        ec2_client.modify_instance_attribute(InstanceId=inst_id, Attribute='instanceType', Value=new_type)
        # 3. 인스턴스 재시작
        ec2_client.start_instances(InstanceIds=[inst_id])
        return True, f"Instance {inst_id} has been changed to {new_type}. (Region: {region})"
    except Exception as e:
        return False, f"Instance {inst_id} change failed. (Region: {region}): {e}"

def lambda_handler(event, context):
    # Slack의 버튼 이벤트 처리
    if 'body' in event:
        try:
            # base64로 인코딩된 body를 디코드
            body_data = event['body']
            if event.get('isBase64Encoded', False):
                body_data = base64.b64decode(body_data).decode('utf-8')
            body = unquote(body_data)[len('payload='):]
            body = json.loads(body)
            if 'actions' in body and body['actions'] and 'value' in body['actions'][0]:
                value_str = body["actions"][0]["value"]
                value_str = re.sub(r'\+', '', value_str)
                action_value = json.loads(value_str)
                success, message = apply_single_ec2_change(action_value)
                
                # Slack으로 결과 알림
                send_slack_block_message([{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": message}
                }], SLACK_WEBHOOK_URL)
                
                return {
                    "statusCode": 200,
                    "body": json.dumps({"message": message})
                }
            return {}
        except Exception as e:
            return {
                "statusCode": 500,
                "body": json.dumps({"error": str(e)})
            }

    # 1. 최적화 대상 계산
    ec2_suggestions = []
    regions = get_all_regions()
    
    for region in regions:
        try:
            ec2_client = boto3.client('ec2', region_name=region)
            cw_client = boto3.client('cloudwatch', region_name=region)
            
            for inst in get_running_instances(ec2_client, region):
                avg_cpu, max_cpu = get_cpu_utilization(cw_client, inst['InstanceId'], MONITOR_DURATION)
                new_type = suggest_instance_type(ec2_client, inst['InstanceType'], avg_cpu)
                if new_type:
                    ec2_suggestions.append({
                        'InstanceId': inst['InstanceId'],
                        'NameTag': next((tag['Value'] for tag in inst.get('Tags', []) if tag['Key'] == 'Name'), None),
                        'CurrentType': inst['InstanceType'],
                        'AverageCPU': round(avg_cpu, 2),
                        'MaxCPU': round(max_cpu, 2),
                        'SuggestedType': new_type,
                        'Region': region
                    })
        except Exception as e:
            print(f"리전 {region} 처리 중 오류 발생: {e}")
            continue
    
    print(ec2_suggestions)

    # 2. Slack으로 제안사항 알림
    if ec2_suggestions:
        notify_ec2_suggestions(ec2_suggestions, SLACK_WEBHOOK_URL)
    if not ec2_suggestions:
        send_slack_block_message([{
            "type": "section",
            "text": {"type": "mrkdwn", "text": "No instances need optimization."}
        }], SLACK_WEBHOOK_URL)
    
    # 처리 결과 반환 (로그/테스트 용도)
    return {
        "EC2Suggestions": ec2_suggestions,
    }
