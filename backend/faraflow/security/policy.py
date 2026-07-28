import ipaddress
import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlparse

from faraflow.domain.schemas import ApprovalPolicy


class PolicyViolation(PermissionError):
    pass


class DomainPolicy:
    SAFE_SCHEMES = {"http", "https", "about", "data", "blob"}

    def __init__(
        self, allowed_domains: Iterable[str], allow_private_networks: bool = False
    ) -> None:
        self.allowed_domains = tuple(
            domain.strip().lower().rstrip(".") for domain in allowed_domains if domain.strip()
        )
        self.allow_private_networks = allow_private_networks

    def assert_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in self.SAFE_SCHEMES:
            raise PolicyViolation(f"blocked URL scheme: {parsed.scheme or '<empty>'}")
        if parsed.scheme in {"about", "data", "blob"}:
            return
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            raise PolicyViolation("URL does not contain a hostname")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not self.allow_private_networks:
            if address.is_private or address.is_loopback or address.is_link_local:
                raise PolicyViolation(f"private network target blocked: {host}")
        if not any(
            host == domain or host.endswith(f".{domain}") for domain in self.allowed_domains
        ):
            raise PolicyViolation(f"domain is not in session allow-list: {host}")


@dataclass(frozen=True)
class TargetRisk:
    kind: str
    label: str
    reason: str


class CriticalActionPolicy:
    _PATTERNS = {
        "delete": re.compile(
            r"\b(delete|remove|erase|destroy|cancel account)\b|删除|移除|注销|清空",
            re.IGNORECASE,
        ),
        "purchase": re.compile(
            r"\b(pay|purchase|buy now|place order|checkout|book now|confirm booking)\b|"
            r"支付|购买|下单|结算|确认订单|确认预订",
            re.IGNORECASE,
        ),
        "send": re.compile(
            r"\b(send|publish|post|reply|submit application)\b|发送|发布|回复|提交申请",
            re.IGNORECASE,
        ),
        "sign_in": re.compile(
            r"\b(sign in|log in|login|authorize|connect account)\b|登录|授权|绑定账号",
            re.IGNORECASE,
        ),
        "submit": re.compile(
            r"\b(submit|confirm|complete|finish|save changes)\b|提交|确认|完成|保存更改",
            re.IGNORECASE,
        ),
    }

    def classify(self, label: str) -> Optional[TargetRisk]:
        compact = " ".join(label.split())[:500]
        for kind, pattern in self._PATTERNS.items():
            if pattern.search(compact):
                return TargetRisk(
                    kind=kind,
                    label=compact,
                    reason=f"目标控件疑似不可逆操作（{kind}），需要策略审批",
                )
        return None

    @staticmethod
    def requires_approval(risk: TargetRisk, policy: ApprovalPolicy) -> bool:
        mapping = {
            "delete": policy.before_delete,
            "purchase": policy.before_purchase,
            "send": policy.before_send,
            "sign_in": policy.before_sign_in,
            "submit": policy.before_submit,
        }
        return mapping.get(risk.kind, True)


class PromptInjectionGuard:
    _SUSPICIOUS = re.compile(
        r"(ignore (all|any|the) (previous|system) instructions|"
        r"reveal (the )?(system prompt|secret|api key)|"
        r"upload .*?(credential|cookie|token)|"
        r"忽略.{0,12}(系统|之前).{0,8}(指令|规则)|"
        r"(泄露|显示).{0,8}(密钥|口令|系统提示词))",
        re.IGNORECASE,
    )

    def scan(self, visible_text: str) -> Optional[str]:
        match = self._SUSPICIOUS.search(visible_text[:50_000])
        if match:
            return match.group(0)[:300]
        return None
