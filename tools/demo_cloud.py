"""Start a mock AWS (moto) pre-loaded with a few resources, so the Cloud tab has something to show.

    pip install "moto[server]"
    python tools/demo_cloud.py

Then, in another terminal, start Sentinel pointed at it (the script prints the exact line).
Nothing here touches a real AWS account: the credentials are fake and every call goes to localhost.
"""
import os
import sys
import time

PORT = int(os.environ.get("DEMO_AWS_PORT", "5555"))
ENDPOINT = f"http://127.0.0.1:{PORT}"

try:
    from moto.server import ThreadedMotoServer
except ImportError:
    sys.exit('moto is not installed. Run:  pip install "moto[server]"')

os.environ.update(AWS_ACCESS_KEY_ID="demo", AWS_SECRET_ACCESS_KEY="demo", AWS_DEFAULT_REGION="ap-south-1")
import boto3  # noqa: E402  (after moto, so the fake credentials are in place)


def seed() -> None:
    def client(service, region="ap-south-1"):
        return boto3.client(service, region_name=region, endpoint_url=ENDPOINT)

    ec2 = client("ec2")
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1, InstanceType="t3.micro",
                      TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "web-server"}]}])
    stopped = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1, InstanceType="t3.small",
                                TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "old-worker"}]}])
    ec2.stop_instances(InstanceIds=[stopped["Instances"][0]["InstanceId"]])
    ec2.create_volume(AvailabilityZone="ap-south-1a", Size=100)      # unattached disk -> "wasted" finding
    ec2.allocate_address(Domain="vpc")                               # unused Elastic IP -> "wasted" finding
    far = client("ec2", "us-west-2")                                 # instance outside your regions -> critical
    far.run_instances(ImageId=far.describe_images()["Images"][0]["ImageId"], MinCount=1, MaxCount=1)
    s3 = client("s3")
    for name in ("demo-backups", "demo-assets"):
        s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})


if __name__ == "__main__":
    server = ThreadedMotoServer(port=PORT)
    server.start()
    seed()
    print(f"Mock AWS running at {ENDPOINT} with demo resources. Leave this window open (Ctrl+C to stop).\n")
    print("In another terminal, from the project folder:\n")
    print(f'  AWS_ENDPOINT_URL={ENDPOINT} AWS_ACCESS_KEY_ID=demo AWS_SECRET_ACCESS_KEY=demo \\')
    print('  AWS_DEFAULT_REGION=ap-south-1 DEMO_API_KEY=demo SENTINEL_DB_PATH=demo.db \\')
    print('  SENTINEL_CONFIG=sentinel.demo.yaml python main.py\n')
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()