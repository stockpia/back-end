import os
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
import base64

# .env 파일에서 암호화 키를 가져오거나, 없으면 유효한 32바이트 기본 키를 사용합니다.
ENCRYPTION_KEY_STR = os.environ.get('ENCRYPTION_KEY', 'a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6')
ENCRYPTION_KEY = ENCRYPTION_KEY_STR.encode('utf-8')

if len(ENCRYPTION_KEY) != 32:
    raise ValueError("ENCRYPTION_KEY must be 32 bytes long for AES-256.")

def encrypt(plain_text: str) -> bytes:
    """
    문자열을 AES-GCM 방식으로 암호화합니다.
    결과: nonce + ciphertext + tag (base64 인코딩된 바이트)
    """
    if not plain_text:
        return b''
    
    plain_text_bytes = plain_text.encode('utf-8')
    
    cipher = AES.new(ENCRYPTION_KEY, AES.MODE_GCM)
    nonce = cipher.nonce
    ciphertext, tag = cipher.encrypt_and_digest(plain_text_bytes)
    
    # nonce, ciphertext, tag를 순서대로 합쳐서 반환
    encrypted_data = nonce + ciphertext + tag
    return base64.b64encode(encrypted_data)

def decrypt(encrypted_data_b64: bytes) -> str:
    """
    AES-GCM으로 암호화된 데이터를 복호화합니다.
    """
    if not encrypted_data_b64:
        return ''

    try:
        encrypted_data = base64.b64decode(encrypted_data_b64)
        
        # nonce, ciphertext, tag 분리
        nonce = encrypted_data[:16]
        tag = encrypted_data[-16:]
        ciphertext = encrypted_data[16:-16]

        cipher = AES.new(ENCRYPTION_KEY, AES.MODE_GCM, nonce=nonce)
        decrypted_bytes = cipher.decrypt_and_verify(ciphertext, tag)
        
        return decrypted_bytes.decode('utf-8')
    except (ValueError, KeyError, TypeError) as e:
        # 복호화 실패 시 (예: 데이터 손상, 잘못된 키) 빈 문자열 반환
        print(f"Decryption failed: {e}")
        return ''
