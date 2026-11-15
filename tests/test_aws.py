from datetime import datetime, timezone

from sentinel.collectors.aws import AwsCollector
from sentinel.config import AwsConfig

NOW = datetime(2027, 3, 15, 12, 0, tzinfo=timezone.utc)


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self):
        return iter(self.pages)


class FakeEC2:
    def __init__(self, region, world):
        self.region, self.w = region, world

    def get_paginator(self, name):
        assert name == "describe_instances"
        insts = self.w["instances"].get(self.region, [])
        return FakePaginator([{"Reservations": [{"Instances": insts}]}])

    def describe_volumes(self, **_):
        return {"Volumes": self.w["volumes"].get(self.region, [])}

    def describe_addresses(self):
        return {"Addresses": self.w["addresses"].get(self.region, [])}

    def describe_regions(self):
        return {"Regions": [{"RegionName": r} for r in self.w["all_regions"]]}

    def describe_instances(self, **_):
        insts = [i for i in self.w["instances"].get(self.region, []) if i["State"]["Name"] in ("running", "pending")]
        return {"Reservations": [{"Instances": insts}] if insts else []}


class FakeCW:
    def __init__(self, region, world):
        self.w = world

    def get_metric_statistics(self, Namespace, MetricName, Dimensions, **_):
        dim = {d["Name"]: d["Value"] for d in Dimensions}
        if Namespace == "AWS/EC2":
            cpu = self.w["cpu"].get(dim["InstanceId"])
            return {"Datapoints": [{"Average": cpu}] * 3} if cpu is not None else {"Datapoints": []}
        bucket = dim["BucketName"]
        val = self.w["s3"][bucket][MetricName]
        return {"Datapoints": [{"Timestamp": NOW, "Average": val}]}


class FakeS3:
    def __init__(self, world):
        self.w = world

    def list_buckets(self):
        return {"Buckets": [{"Name": n} for n in self.w["s3"]]}

    def get_bucket_location(self, Bucket):
        return {"LocationConstraint": "ap-south-1"}


class FakeCE:
    def __init__(self, world):
        self.w = world

    def get_cost_and_usage(self, **_):
        if self.w.get("ce_fail"):
            raise RuntimeError("AccessDenied: account 123456789012")
        groups = [{"Keys": [s], "Metrics": {"UnblendedCost": {"Amount": str(a)}}} for s, a in self.w["costs"].items()]
        return {"ResultsByTime": [{"Groups": groups}]}

    def get_cost_forecast(self, **_):
        return {"Total": {"Amount": str(self.w["remaining_forecast"])}}


def make(world, **cfg_kwargs):
    cfg = AwsConfig(enabled=True, regions=["ap-south-1"], **cfg_kwargs)

    def factory(service, region):
        return {"ec2": lambda: FakeEC2(region, world), "cloudwatch": lambda: FakeCW(region, world),
                "s3": lambda: FakeS3(world), "ce": lambda: FakeCE(world)}[service]()

    return AwsCollector(cfg, client_factory=factory, now=lambda: NOW)


def world(**over):
    w = {
        "instances": {"ap-south-1": [
            {"InstanceId": "i-busy", "State": {"Name": "running"}, "InstanceType": "t3.micro",
             "Tags": [{"Key": "Name", "Value": "web"}]},
            {"InstanceId": "i-idle", "State": {"Name": "running"}, "InstanceType": "t3.large", "Tags": []},
            {"InstanceId": "i-off", "State": {"Name": "stopped"}, "InstanceType": "t3.small"},
        ]},
        "cpu": {"i-busy": 42.0, "i-idle": 1.2},
        "volumes": {"ap-south-1": [{"VolumeId": "vol-1", "Size": 100}]},
        "addresses": {"ap-south-1": [{"PublicIp": "1.2.3.4", "AllocationId": "eip-1"},
                                     {"PublicIp": "5.6.7.8", "AllocationId": "eip-2", "AssociationId": "assoc"}]},
        "all_regions": ["ap-south-1", "us-west-2"],
        "s3": {"my-bucket": {"BucketSizeBytes": 5 * 1024 ** 3, "NumberOfObjects": 1234}},
        "costs": {"Amazon EC2": 30.0, "Amazon S3": 5.5, "AWS Lambda": 0.5},
        "remaining_forecast": 40.0,
    }
    w.update(over)
    return w


def ids(data):
    return {f["id"] for f in data["findings"]}


def test_ec2_inventory_and_findings():
    data = make(world()).collect()
    assert {i["id"]: i["state"] for i in data["instances"]} == {"i-busy": "running", "i-idle": "running", "i-off": "stopped"}
    assert next(i for i in data["instances"] if i["id"] == "i-busy")["name"] == "web"
    found = ids(data)
    assert "idle-ec2:i-idle" in found and "idle-ec2:i-busy" not in found
    assert "stopped-ec2:i-off" in found
    assert "ebs:vol-1" in found
    assert "eip:eip-1" in found and "eip:eip-2" not in found  # only the unassociated IP is flagged


def test_s3_usage():
    data = make(world()).collect()
    assert data["buckets"] == [{"name": "my-bucket", "size_gb": 5.0, "objects": 1234}]


def test_cost_and_budget_warning_then_critical():
    data = make(world(), monthly_budget_usd=50).collect()
    c = data["cost"]
    assert c["month_to_date_usd"] == 36.0 and c["forecast_usd"] == 76.0 and c["budget_used_percent"] == 72.0
    assert c["top_services"][0] == {"service": "Amazon EC2", "usd": 30.0}
    budget = next(f for f in data["findings"] if f["id"] == "budget")
    assert budget["severity"] == "warning"  # forecast passes 80% of budget

    over = make(world(), monthly_budget_usd=30).collect()
    assert next(f for f in over["findings"] if f["id"] == "budget")["severity"] == "critical"

    no_budget = make(world()).collect()
    assert "budget" not in ids(no_budget)


def test_unexpected_region_flagged():
    w = world()
    w["instances"]["us-west-2"] = [{"InstanceId": "i-rogue", "State": {"Name": "running"}}]
    data = make(w).collect()
    assert "unexpected-region:us-west-2" in ids(data)
    assert next(f for f in data["findings"] if f["id"].startswith("unexpected"))["severity"] == "critical"
    assert "unexpected-region:us-west-2" not in ids(make(w, flag_unexpected_regions=False).collect())


def test_partial_failure_is_isolated_and_redacted():
    data = make(world(ce_fail=True)).collect()
    assert data["cost"] is None and len(data["instances"]) == 3  # EC2 data survives a Cost Explorer failure
    assert data["errors"] == ["cost: RuntimeError"]
    assert "123456789012" not in repr(data["errors"])  # only the exception class, never its message
