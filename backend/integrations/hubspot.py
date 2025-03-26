# hubspot.py

import json
import secrets
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import base64
from integrations.integration_item import IntegrationItem

from redis_client import add_key_value_redis, get_value_redis, delete_key_redis

CLIENT_ID = '577b9348-0f00-40cd-a921-a3753b2be02d'
CLIENT_SECRET = '3c3b3d3d-c043-464a-b07a-77325af305d3'
REDIRECT_URI = 'http://localhost:8000/integrations/hubspot/oauth2callback'
SCOPE = 'crm.objects.contacts.read'

# HubSpot OAuth URLs
AUTHORIZATION_URL = f'https://app.hubspot.com/oauth/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&scope={SCOPE}'
TOKEN_URL = 'https://api.hubspot.com/oauth/v1/token'

async def authorize_hubspot(user_id, org_id):
    state_data = {
        'state': secrets.token_urlsafe(32),
        'user_id': user_id,
        'org_id': org_id
    }
    encoded_state = json.dumps(state_data)
    await add_key_value_redis(f'hubspot_state:{org_id}:{user_id}', encoded_state, expire=600)

    return f'{AUTHORIZATION_URL}&state={encoded_state}'

async def oauth2callback_hubspot(request: Request):
    if request.query_params.get('error'):
        raise HTTPException(status_code=400, detail=request.query_params.get('error'))
    
    code = request.query_params.get('code')
    encoded_state = request.query_params.get('state')
    
    if not code or not encoded_state:
        raise HTTPException(status_code=400, detail='Missing code or state parameter')
    
    state_data = json.loads(encoded_state)
    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')

    saved_state = await get_value_redis(f'hubspot_state:{org_id}:{user_id}')

    if not saved_state or original_state != json.loads(saved_state).get('state'):
        raise HTTPException(status_code=400, detail='State does not match.')

    # Exchange code for access token
    async with httpx.AsyncClient() as client:
        response, _ = await asyncio.gather(
            client.post(
                TOKEN_URL,
                data={
                    'grant_type': 'authorization_code',
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET,
                    'redirect_uri': REDIRECT_URI,
                    'code': code
                }
            ),
            delete_key_redis(f'hubspot_state:{org_id}:{user_id}')
        )

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail='Failed to obtain access token')

    # Store credentials in Redis
    await add_key_value_redis(f'hubspot_credentials:{org_id}:{user_id}', json.dumps(response.json()), expire=3600)
    
    # Return HTML response to close the window
    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """
    return HTMLResponse(content=close_window_script)

async def get_hubspot_credentials(user_id, org_id):
    credentials = await get_value_redis(f'hubspot_credentials:{org_id}:{user_id}')
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    
    credentials = json.loads(credentials)
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    
    # Delete credentials from Redis after retrieving
    await delete_key_redis(f'hubspot_credentials:{org_id}:{user_id}')

    return credentials

async def create_integration_item_metadata_object(response_json):
    """Creates an IntegrationItem from HubSpot response data"""
    properties = response_json.get('properties', {})
    first_name = properties.get('firstname', '')
    last_name = properties.get('lastname', '')
    email = properties.get('email', '')
    display_name = f"{first_name} {last_name}"
    if not display_name.strip():
        display_name = email if email else f"Contact {response_json.get('id', '')}"
    
    return IntegrationItem(
        id=response_json.get('id'),
        type='hubspot_contact',
        name=display_name,
        creation_time=properties.get('createdate'),
        last_modified_time=properties.get('hs_lastmodifieddate'),
        url=f"https://app.hubspot.com/contacts/{response_json.get('properties', {}).get('hs_object_id')}/contact/{response_json.get('id')}" if response_json.get('id') else None
    )

async def get_items_hubspot(credentials):
    """Retrieves contacts from HubSpot and returns them as IntegrationItems"""
    try:
        creds = json.loads(credentials)
        access_token = creds.get('access_token')
        
        if not access_token:
            raise HTTPException(status_code=400, detail='Invalid credentials')
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                'https://api.hubapi.com/crm/v3/objects/contacts',
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Content-Type': 'application/json'
                },
                params={
                    'limit': 100,
                    'properties': 'firstname,lastname,email,createdate,hs_lastmodifieddate,hs_object_id'
                }
            )
        
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail=f'Failed to fetch contacts from HubSpot: {response.text}')
        
        response_data = response.json()
        items = []
        
        for contact in response_data.get('results', []):
            item = await create_integration_item_metadata_object(contact)
            items.append(item)
        
        print(f"Retrieved {len(items)} items from HubSpot")
        return items
    except Exception as e:
        print(f"Error retrieving HubSpot contacts: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error retrieving HubSpot contacts: {str(e)}")