"""
Rename this file to settings.py
and replace all placeholder values
with actual credentials/configuration.
"""

import logging
import sys
import pyodbc

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

logger = logging.getLogger(__name__)

# AZURE KEY VAULT
# Fetch all sensitive credentials/secrets securely
try:
    credential = DefaultAzureCredential()

    kv = SecretClient(
        vault_url="https://jbrothers-keyvault.vault.azure.net/",
        credential=credential
    )

    # SQL Server password
    SQL_PASSWORD = kv.get_secret("AzureSQL-Password").value

    # SMTP / Mail service password
    MAIL_SERVICE_PASSWORD = kv.get_secret("Mail-Service-Password").value

    # Azure Blob Storage connection string
    BLOB_CONNECTION_STRING = kv.get_secret("market-data-blob-string").value

except Exception as e:
    logger.critical(f"Failed to fetch secrets from Azure Key Vault: {e}", exc_info=True)
    sys.exit(1)

container_name = "data"
 
#sql server details
server     = "jbrothers-sql-server.database.windows.net"
database   = "AZ-NM-database"
username   = "jbrotherssql"
table_name = "dbo.market_data"
view_name  = "dbo.vw_market_data"
 
#credentials for email service
EMAIL_FROM = "jbrrothers3@gmail.com"
 
# mail ids to receive the market data report
recipients = [
    # "savanp@jbrrothers.com",
    # "Krutalit@jbrrothers.com",
    # "pramitp@jbrrothers.com",
    "rencyk@jbrrothers.com",
    "drashtic@jbrrothers.com",
    # "Krushangm@jbrrothers.com",
    # "vansha@jbrrothers.com",
    # "anuragt@jbrrothers.com",
    # "stephenb@jbrrothers.com",
    # "varuns@jbrrothers.com",
    # "alexar@jbrrothers.com",
    # "Jonathang@jbrrothers.com",
    # "Caitlynt@divine1.co",
    # "Connorp@jananii.com",
    # "stephenb@jbrrothers.com",
    # "hemp@jbrrothers.com",
    # "hitayua@jbrrothers.com"
   
]



