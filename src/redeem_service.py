import math
import logging
import requests
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from src.config import Settings

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

RPC_URLS = [
    "https://polygon-bor.publicnode.com", # Server Best
    "https://1rpc.io/matic", # Local Best
    "https://polygon-rpc.com",
    "https://rpc-mainnet.maticvigil.com",
]

def connect_to_polygon():
    for url in RPC_URLS:
        logger.info(f"Trying to connect to {url}...")
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 30}))

        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        try:
            block = w3.eth.get_block_number()
            logger.info(f"✅ Connected! Current Block: {block}")
            return w3
        except Exception as e:
            logger.error(f"❌ Failed to connect to {url}: {e}")
            continue
    return None

def get_redeemable_positions(settings: Settings):
    """Checks Polymarket API for finished winning markets"""
    url = f"https://data-api.polymarket.com/positions?user={settings.funder}&redeemable=true"
    try:
        res = requests.get(url)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        logger.info(f"Could not fetch positions: {e}")
        return []

def redeem_via_proxy(settings: Settings, condition_id):
    ctf_abi = [{"name":"redeemPositions","type":"function","inputs":[
        {"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},
        {"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}
    ],"outputs":[]}]
    ctf_contract = w3.eth.contract(address=settings.ctf_address, abi=ctf_abi)

    inner_data = ctf_contract.encode_abi("redeemPositions", args=[
        settings.usdc_address, "0x" + "0" * 64, condition_id, [1, 2]
    ])

    proxy_abi = [
        {
            "name": "execTransaction",
            "type": "function",
            "stateMutability": "payable",
            "inputs": [
                {"name": "to",             "type": "address"},
                {"name": "value",          "type": "uint256"},
                {"name": "data",           "type": "bytes"},
                {"name": "operation",      "type": "uint8"},
                {"name": "safeTxGas",      "type": "uint256"},
                {"name": "baseGas",        "type": "uint256"},
                {"name": "gasPrice",       "type": "uint256"},
                {"name": "gasToken",       "type": "address"},
                {"name": "refundReceiver", "type": "address"},
                {"name": "signatures",     "type": "bytes"}
            ],
            "outputs": [
                {"name": "success",        "type": "bool"}
            ]
        }
    ]  
    proxy_contract = w3.eth.contract(address=settings.funder, abi=proxy_abi)

    signature = "0x000000000000000000000000" + settings.metamask_address[2:].lower() + "0000000000000000000000000000000000000000000000000000000000000000" + "01"

    try:
        tx = proxy_contract.functions.execTransaction(
            settings.ctf_address, 0, inner_data, 0, 0, 0, 0, 
            "0x0000000000000000000000000000000000000000",
            "0x0000000000000000000000000000000000000000",
            signature
        ).build_transaction({
            'from': settings.metamask_address,
            'nonce': w3.eth.get_transaction_count(settings.metamask_address),
            'gas': 400000, 
            'gasPrice': w3.eth.gas_price
        })

        signed = w3.eth.account.sign_transaction(tx, settings.private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info(f"✅ Success! Transaction: https://polygonscan.com/tx/{tx_hash.hex()}")
    except Exception as e:
        logger.error(f"❌ Failed: {e}")
