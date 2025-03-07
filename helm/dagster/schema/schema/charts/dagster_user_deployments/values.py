from typing import List

from pydantic import BaseModel  # pylint: disable=no-name-in-module

from ..dagster.subschema import Global, ServiceAccount
from ..utils import kubernetes
from .subschema.user_deployments import UserDeployment


class DagsterUserDeploymentsHelmValues(BaseModel):
    __doc__ = "@" + "generated"

    deployments: List[UserDeployment]
    imagePullSecrets: List[kubernetes.SecretRef]
    serviceAccount: ServiceAccount
