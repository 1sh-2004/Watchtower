"""Read-only AWS monitoring: EC2, S3, estimated spend and wasted resources.

Only Describe/List/Get calls are made (see infra/aws-readonly-policy.json).
Nothing is ever started, stopped, resized or deleted.

Cost note: the Cost Explorer API bills about $0.01 per request, so spend is
polled on a slow interval (default every 6 hours).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ..config import AwsConfig

log = logging.getLogger("sentinel.aws")

ClientFactory = Callable[[str, Optional[str]], Any]  # (service, region) -> boto3-like client


def default_client_factory(profile: Optional[str], access_key_id: Optional[str] = None,
                           secret_access_key: Optional[str] = None,
                           session_token: Optional[str] = None) -> ClientFactory:
    import boto3  # imported lazily so the app runs without AWS configured

    if access_key_id and secret_access_key:
        # Explicit keys (entered in the Settings tab) win over any profile or environment.
        session = boto3.Session(aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,
                                aws_session_token=session_token or None)
    elif profile:
        session = boto3.Session(profile_name=profile)
    else:
        session = boto3.Session()

    def make(service: str, region: Optional[str] = None):
        return session.client(service, region_name=region)

    return make


def describe_error(exc: BaseException) -> str:
    """Safe text for an error: the class, plus the AWS error *code* when there is one
    (e.g. ``ClientError: UnauthorizedOperation``). Never the message, which can carry
    account IDs, ARNs or key fragments."""
    code = None
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
    if code and isinstance(code, str) and code.replace(".", "").replace("_", "").isalnum():
        return f"{type(exc).__name__}: {code}"
    return type(exc).__name__


def _name_tag(tags: Optional[List[Dict[str, str]]]) -> str:
    for t in tags or []:
        if t.get("Key") == "Name":
            return t.get("Value", "")
    return ""


class AwsCollector:
    def __init__(self, cfg: AwsConfig, client_factory: Optional[ClientFactory] = None,
                 now: Optional[Callable[[], datetime]] = None) -> None:
        self.cfg = cfg
        self._factory = client_factory
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def factory(self) -> ClientFactory:
        if self._factory is None:
            self._factory = default_client_factory(self.cfg.profile, self.cfg.access_key_id,
                                                   self.cfg.secret_access_key, self.cfg.session_token)
        return self._factory

    # ---- EC2 ------------------------------------------------------------
    def _avg_cpu(self, cw, instance_id: str) -> Optional[float]:
        end = self._now()
        resp = cw.get_metric_statistics(
            Namespace="AWS/EC2", MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=end - timedelta(days=self.cfg.idle_days), EndTime=end,
            Period=86400, Statistics=["Average"],
        )
        points = [p["Average"] for p in resp.get("Datapoints", [])]
        return round(sum(points) / len(points), 2) if points else None

    def collect_ec2(self, region: str) -> Dict[str, Any]:
        ec2 = self.factory("ec2", region)
        cw = self.factory("cloudwatch", region)
        instances, findings = [], []

        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for res in page.get("Reservations", []):
                for inst in res.get("Instances", []):
                    state = inst["State"]["Name"]
                    item = {
                        "id": inst["InstanceId"], "name": _name_tag(inst.get("Tags")),
                        "type": inst.get("InstanceType"), "state": state, "region": region,
                        "avg_cpu_percent": None,
                    }
                    if state == "running":
                        try:
                            item["avg_cpu_percent"] = self._avg_cpu(cw, inst["InstanceId"])
                        except Exception as exc:  # noqa: BLE001 - one bad call must not kill the sweep
                            log.warning("cpu lookup failed for %s: %s", inst["InstanceId"], type(exc).__name__)
                        cpu = item["avg_cpu_percent"]
                        if cpu is not None and cpu < self.cfg.idle_cpu_percent:
                            findings.append({
                                "id": f"idle-ec2:{inst['InstanceId']}", "severity": "warning",
                                "message": (f"EC2 {inst['InstanceId']} ({item['name'] or 'unnamed'}, {region}) averaged "
                                            f"{cpu}% CPU over {self.cfg.idle_days} days: likely idle"),
                            })
                    elif state == "stopped":
                        findings.append({
                            "id": f"stopped-ec2:{inst['InstanceId']}", "severity": "info",
                            "message": (f"EC2 {inst['InstanceId']} ({item['name'] or 'unnamed'}, {region}) is stopped; "
                                        "its EBS volumes still incur cost"),
                        })
                    instances.append(item)
        return {"instances": instances, "findings": findings}

    def collect_waste(self, region: str) -> List[Dict[str, Any]]:
        """Unattached EBS volumes and unassociated Elastic IPs."""
        ec2 = self.factory("ec2", region)
        findings = []
        vols = ec2.describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}])
        for v in vols.get("Volumes", []):
            findings.append({
                "id": f"ebs:{v['VolumeId']}", "severity": "warning",
                "message": f"EBS volume {v['VolumeId']} ({v.get('Size', '?')} GiB, {region}) is unattached",
            })
        for a in ec2.describe_addresses().get("Addresses", []):
            if not a.get("AssociationId"):
                ip = a.get("PublicIp", "?")
                findings.append({
                    "id": f"eip:{a.get('AllocationId', ip)}", "severity": "warning",
                    "message": f"Elastic IP {ip} ({region}) is not associated with anything and is billed",
                })
        return findings

    def collect_unexpected_regions(self) -> List[Dict[str, Any]]:
        """Running instances in regions outside the configured allow-list."""
        ec2 = self.factory("ec2", self.cfg.regions[0])
        allowed = set(self.cfg.regions)
        findings = []
        for r in ec2.describe_regions().get("Regions", []):
            region = r["RegionName"]
            if region in allowed:
                continue
            try:
                client = self.factory("ec2", region)
                resp = client.describe_instances(
                    Filters=[{"Name": "instance-state-name", "Values": ["running", "pending"]}], MaxResults=5
                )
            except Exception:  # noqa: BLE001 - opt-in regions may be disabled for the account
                continue
            count = sum(len(res.get("Instances", [])) for res in resp.get("Reservations", []))
            if count:
                findings.append({
                    "id": f"unexpected-region:{region}", "severity": "critical",
                    "message": f"{count}+ running EC2 instance(s) in unexpected region {region}",
                })
        return findings

    # ---- S3 -------------------------------------------------------------
    def collect_s3(self) -> List[Dict[str, Any]]:
        s3 = self.factory("s3", None)
        buckets = s3.list_buckets().get("Buckets", [])[: self.cfg.max_s3_buckets]
        out = []
        end = self._now()
        for b in buckets:
            name = b["Name"]
            item = {"name": name, "size_gb": None, "objects": None}
            try:
                loc = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
                cw = self.factory("cloudwatch", loc)

                def latest(metric: str, storage_type: str) -> Optional[float]:
                    resp = cw.get_metric_statistics(
                        Namespace="AWS/S3", MetricName=metric,
                        Dimensions=[{"Name": "BucketName", "Value": name},
                                    {"Name": "StorageType", "Value": storage_type}],
                        StartTime=end - timedelta(days=3), EndTime=end, Period=86400, Statistics=["Average"],
                    )
                    pts = sorted(resp.get("Datapoints", []), key=lambda p: p["Timestamp"])
                    return pts[-1]["Average"] if pts else None

                size = latest("BucketSizeBytes", "StandardStorage")
                objs = latest("NumberOfObjects", "AllStorageTypes")
                item["size_gb"] = round(size / 1024 ** 3, 3) if size is not None else None
                item["objects"] = int(objs) if objs is not None else None
            except Exception as exc:  # noqa: BLE001
                log.warning("s3 metrics failed for a bucket: %s", type(exc).__name__)
            out.append(item)
        return out

    # ---- Cost -----------------------------------------------------------
    def collect_cost(self) -> Dict[str, Any]:
        ce = self.factory("ce", "us-east-1")
        today = self._now().date()
        start = today.replace(day=1)
        end = today + timedelta(days=1)  # CE end date is exclusive

        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY", Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        by_service: Dict[str, float] = {}
        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
                by_service[g["Keys"][0]] = by_service.get(g["Keys"][0], 0.0) + amount
        mtd = round(sum(by_service.values()), 2)
        top = sorted(by_service.items(), key=lambda kv: kv[1], reverse=True)[:5]

        forecast = None
        next_month = (start + timedelta(days=32)).replace(day=1)
        if today < next_month - timedelta(days=1):
            try:
                f = ce.get_cost_forecast(
                    TimePeriod={"Start": end.isoformat(), "End": next_month.isoformat()},
                    Metric="UNBLENDED_COST", Granularity="MONTHLY",
                )
                forecast = round(mtd + float(f["Total"]["Amount"]), 2)
            except Exception as exc:  # noqa: BLE001 - CE refuses forecasts for young accounts
                log.info("cost forecast unavailable: %s", type(exc).__name__)
        else:
            forecast = mtd

        budget = self.cfg.monthly_budget_usd
        return {
            "month_to_date_usd": mtd,
            "forecast_usd": forecast,
            "budget_usd": budget or None,
            "budget_used_percent": round(100 * mtd / budget, 1) if budget else None,
            "top_services": [{"service": s, "usd": round(a, 2)} for s, a in top],
        }

    # ---- orchestration ---------------------------------------------------
    def collect(self) -> Dict[str, Any]:
        """Run every collector. Each part fails independently and reports its error."""
        data: Dict[str, Any] = {"enabled": True, "instances": [], "buckets": [], "cost": None,
                                "findings": [], "errors": []}

        def guarded(label: str, fn):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                # Only the exception class: messages from AWS can include account details.
                data["errors"].append(f"{label}: {describe_error(exc)}")
                log.warning("aws %s failed: %s", label, describe_error(exc))
                return None

        for region in self.cfg.regions:
            res = guarded(f"ec2[{region}]", lambda r=region: self.collect_ec2(r))
            if res:
                data["instances"] += res["instances"]
                data["findings"] += res["findings"]
            waste = guarded(f"waste[{region}]", lambda r=region: self.collect_waste(r))
            if waste:
                data["findings"] += waste

        if self.cfg.flag_unexpected_regions:
            data["findings"] += guarded("regions", self.collect_unexpected_regions) or []

        data["buckets"] = guarded("s3", self.collect_s3) or []
        data["cost"] = guarded("cost", self.collect_cost)

        cost = data["cost"]
        if cost and cost.get("budget_usd"):
            warn_at = cost["budget_usd"] * self.cfg.budget_warn_pct / 100
            basis = max(cost["month_to_date_usd"], cost.get("forecast_usd") or 0)
            if cost["month_to_date_usd"] >= cost["budget_usd"]:
                data["findings"].append({
                    "id": "budget", "severity": "critical",
                    "message": f"Spend ${cost['month_to_date_usd']} has exceeded the ${cost['budget_usd']} monthly budget",
                })
            elif basis >= warn_at:
                data["findings"].append({
                    "id": "budget", "severity": "warning",
                    "message": (f"Spend ${cost['month_to_date_usd']} (forecast ${cost.get('forecast_usd')}) is past "
                                f"{self.cfg.budget_warn_pct:.0f}% of the ${cost['budget_usd']} monthly budget"),
                })
        return data
