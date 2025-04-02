# app.py
from flask import Flask, request, render_template, redirect, url_for
from flask_restx import Api, Resource, fields
import uuid
import json
import os
from datetime import datetime
import pandas as pd
import openai
from urllib.parse import urlparse
from dotenv import load_dotenv

app = Flask(__name__)

# Initialize Flask-RESTX API with doc='/swagger' to set Swagger UI at /swagger
api = Api(app,
          version='1.0',
          title='Type 86 Shipment API',
          description='API for processing Type 86 shipments with fallback to Type 11',
          doc='/swagger')  # Set Swagger UI at /swagger

# Set base directory and data directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# Load environment variables
load_dotenv()

# Define paths for data files
CUSTOMERS_FILE = os.path.join(DATA_DIR, "customers.json")
SHIPMENTS_FILE = os.path.join(DATA_DIR, "shipments.json")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

# Ensure the data and upload directories exist
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# Initialize the JSON files if they don't exist
if not os.path.exists(CUSTOMERS_FILE):
    with open(CUSTOMERS_FILE, 'w') as f:
        json.dump({}, f)

if not os.path.exists(SHIPMENTS_FILE):
    with open(SHIPMENTS_FILE, 'w') as f:
        json.dump([], f)


# Helper functions to read/write JSON files
def read_customers():
    with open(CUSTOMERS_FILE, 'r') as f:
        return json.load(f)


def write_customers(customers):
    with open(CUSTOMERS_FILE, 'w') as f:
        json.dump(customers, f, indent=2)


def read_shipments():
    with open(SHIPMENTS_FILE, 'r') as f:
        return json.load(f)


def write_shipments(shipments):
    with open(SHIPMENTS_FILE, 'w') as f:
        json.dump(shipments, f, indent=2)


# Mock HS Classification Service (returns a 10-digit HTS code)
def mock_hs_classification(description):
    if "t-shirt" in description.lower():
        return "6109100010"  # HTS code for cotton t-shirts
    elif "electronics" in description.lower():
        return "8517620090"  # HTS code for electronics
    elif "lipstick" in description.lower():
        return "3403115000"  # HTS code for lipstick
    else:
        return "9999999999"  # Fallback HTS code


# Mock PGA Flags Service (returns applicable PGA agencies)
def mock_pga_flags(hts_code):
    # Only return PGA flags for specific HS codes
    if hts_code.startswith("61"):
        return []  # No PGA flags for cotton t-shirts in mock
    elif hts_code.startswith("85"):
        return []  # No PGA flags for electronics in mock
    elif hts_code == "3403115000":  # Lipstick triggers CPSC in mock
        return ["CPSC"]
    else:
        return []  # No PGA flags by default


# Mock Denied Party List Screening
def check_denied_party_list(consignee_name):
    denied_parties = ["John Smith", "Evil Corp"]  # Mock denied party list
    return consignee_name in denied_parties


# Ported from main.py: Utility function to validate URLs
def is_valid_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# Ported from main.py: PGA Lookup Logic
def lookup_pga_requirements(hs_code, name, description):
    target = hs_code

    # HS Chapters
    df_chapters = pd.read_excel(os.path.join(DATA_DIR, "HS_Chapters_lookup.xlsx"))
    df_chapters["Chapter"] = df_chapters["Chapter"].astype(str).str.zfill(2)
    df_chapters = df_chapters.ffill().bfill()
    chapter_key = target[:2].zfill(2)
    chapters = df_chapters[df_chapters["Chapter"] == chapter_key] \
        .replace("", pd.NA).dropna(axis=1, how="all") \
        .to_dict(orient="records")

    # PGA_HTS + PGA_Codes
    df_hts = pd.read_excel(os.path.join(DATA_DIR, "PGA_HTS.xlsx"), dtype=str) \
        .rename(columns={"HTS Number - Full": "HsCode"})
    df_pga = pd.read_excel(os.path.join(DATA_DIR, "PGA_codes.xlsx"))
    pga_hts = (
        df_hts.merge(df_pga, how="left",
                     left_on=["PGA Name Code", "PGA Flag Code", "PGA Program Code"],
                     right_on=["Agency Code", "Code", "Program Code"])
        .replace("", pd.NA).dropna(axis=1, how="all")
    )
    pga_hts = pga_hts[pga_hts["HsCode"] == target].to_dict(orient="records")

    # HS Rules
    sheets = pd.read_excel(os.path.join(DATA_DIR, "hs_codes.xlsx"), sheet_name=None)
    df_rules = pd.concat(sheets.values(), ignore_index=True)
    df_rules["HsCode"] = df_rules["HsCode"].astype(str)
    df_rules["Chapter"] = df_rules["HsCode"].str[:2].str.zfill(2)
    df_rules["Header"] = df_rules["HsCode"].str[:4]
    hs_rules = df_rules[df_rules["HsCode"].str.startswith(target)]
    if hs_rules.empty:
        hs_rules = df_rules[df_rules["HsCode"].str.startswith(target[:4])]
    if hs_rules.empty:
        hs_rules = df_rules[df_rules["Chapter"] == chapter_key]
    hs_rules = hs_rules.replace("", pd.NA).dropna(axis=1, how="all") \
        .to_dict(orient="records")

    # Build a unique set of only valid HTTP(S) links
    links = set()
    for rec in pga_hts:
        for col in ("TextLink", "Website Link", "CFR"):
            raw = rec.get(col)
            if raw is None or pd.isna(raw):
                continue
            for url in str(raw).split():
                if is_valid_url(url):
                    links.add(url)

    # Inside lookup_pga_requirements function
    requirements = []
    for url in sorted(links):
        # Mock response instead of calling OpenAI
        requirements.append({
            "url": url,
            "raw_response": "Mocked response: Required documents - Certificate of Compliance, Safety Data Sheet (if applicable).",
            "parsed_requirements": "Mocked response: Required documents - Certificate of Compliance, Safety Data Sheet (if applicable)."
        })

    pga_flags = [rec.get("PGA Name Code") for rec in pga_hts if rec.get("PGA Name Code")]
    return list(set(pga_flags)), {
        "hs_chapters": chapters,
        "pga_hts": pga_hts,
        "hs_rules": hs_rules,
        "pga_requirements": requirements
    }


# Home page (new route /home)
@app.route('/home')
def home():
    return render_template('home.html')


# Onboarding page
@app.route('/onboard', methods=['GET', 'POST'])
def onboard():
    api_token = None
    success = False
    customers = read_customers()
    existing_customer = None
    existing_api_token = None

    # Check if a customer already exists
    if customers:
        existing_api_token = list(customers.keys())[0]  # Get the first customer (for demo purposes)
        existing_customer = customers[existing_api_token]

    if request.method == 'POST':
        # Collect customer data
        company_name = request.form['company_name']

        # Address fields
        address = {
            'street_address_1': request.form['street_address_1'],
            'street_address_2': request.form['street_address_2'],
            'city': request.form['city'],
            'region': request.form['region'],
            'postal_code': request.form['postal_code'],
            'country': request.form['country']
        }

        # Contact fields
        contact = {
            'email': request.form['email'],
            'phone': request.form['phone']
        }

        # Importer of Record and Power of Attorney
        is_importer_of_record = request.form.get('is_importer_of_record') == 'on'
        has_poa = request.form.get('has_poa') == 'on' if not is_importer_of_record else False
        poa_expiry_date = None
        poa_file_path = None

        if has_poa:
            poa_expiry_date = request.form['poa_expiry_date']
            if 'poa_file' in request.files:
                poa_file = request.files['poa_file']
                if poa_file and poa_file.filename.endswith('.pdf'):
                    # Save the file with a unique name
                    filename = f"{uuid.uuid4()}_{poa_file.filename}"
                    poa_file_path = os.path.join(UPLOAD_DIR, filename)
                    poa_file.save(poa_file_path)

        # Status
        status = request.form['status']

        # Generate a unique API token
        api_token = str(uuid.uuid4())

        # Load existing customers and clear them (for demo purposes, we only allow one customer)
        customers = {}

        # Store customer data with the API token
        customers[api_token] = {
            'company_name': company_name,
            'address': address,
            'contact': contact,
            'is_importer_of_record': is_importer_of_record,
            'has_poa': has_poa,
            'poa_expiry_date': poa_expiry_date,
            'poa_file_path': poa_file_path,
            'status': status,
            'created_at': datetime.utcnow().isoformat()
        }

        # Save updated customers to file
        write_customers(customers)
        success = True

        # Update existing customer for display
        existing_customer = customers[api_token]
        existing_api_token = api_token

    return render_template('onboard.html',
                           api_token=api_token,
                           success=success,
                           existing_customer=existing_customer,
                           existing_api_token=existing_api_token)


# Delete customer route
@app.route('/delete-customer/<api_token>', methods=['POST'])
def delete_customer(api_token):
    customers = read_customers()
    if api_token in customers:
        # Delete any uploaded POA file if it exists
        customer = customers[api_token]
        if customer.get('poa_file_path') and os.path.exists(customer['poa_file_path']):
            os.remove(customer['poa_file_path'])
        # Remove the customer
        del customers[api_token]
        write_customers(customers)
    return redirect(url_for('onboard'))


# Redirect /onboard.html to /onboard
@app.route('/onboard.html')
def redirect_onboard():
    return redirect(url_for('onboard'))


# Shipment submission page (for manual testing with scenarios)
@app.route('/shipment/<scenario>', methods=['GET', 'POST'])
@app.route('/shipment', methods=['GET', 'POST'])
def shipment_form(scenario=None):
    error_message = None
    warning_message = None
    success_message = None
    success = False
    response = None
    pga_full_response = None
    api_token = "09a7f994-979d-4cc1-8077-f06c91aa6882"  # Default dummy API token

    # Load customers to validate IOR/POA status
    customers = read_customers()
    customer = customers.get(api_token)

    # Default dummy data for Scenario 1 (under $800, no previous shipments, no PGA flags)
    default_data = {
        'api_token': api_token,
        'shipper_id': "SHIP123",
        'consignee_name': "John Doe",
        'consignee_street_address_1': "123 Main St",
        'consignee_street_address_2': "Apt 4B",
        'consignee_city': "New York",
        'consignee_region': "NY",
        'consignee_postal_code': "10001",
        'consignee_country': "USA",
        'description': "Cotton T-shirt",
        'hs_code': "6109100010",  # No dots
        'quantity': 2,
        'value': 50.00,
        'country_of_origin': "CN",
        'tracking_number': "TRACK123"
    }

    # Scenario 2: Above $800 daily limit due to previous shipments
    if scenario == "above_800_limit":
        default_data['value'] = 500.00  # This shipment alone is under $800
        # Simulate previous shipments
        previous_shipment = {
            'shipment_data': {
                'consignee_name': default_data['consignee_name'],
                'value': 400.00,
                'submitted_at': datetime.utcnow().isoformat()
            }
        }
        shipments = read_shipments()
        shipments.append(previous_shipment)
        write_shipments(shipments)

    # Scenario 3: Customer is not IOR and has no POA
    elif scenario == "no_ior_no_poa":
        if customer:
            customer['is_importer_of_record'] = False
            customer['has_poa'] = False
            customers[api_token] = customer
            write_customers(customers)

    # Scenario 4: Consignee is on denied party list
    elif scenario == "denied_party":
        default_data['consignee_name'] = "John Smith"  # On denied party list

    # Scenario 5: HS Code triggers PGA flag (Lipstick)
    elif scenario == "pga_flag":
        default_data['hs_code'] = "3403115000"  # Lipstick, no dots
        default_data['description'] = "Lipstick - Creamy Matte Finish"

    # Scenario 6: Shipment value is negative (invalid)
    elif scenario == "negative_value":
        default_data['value'] = -50.00

    # Scenario 7: No HS Code provided, assigned by dummy service
    elif scenario == "no_hs_code":
        default_data['hs_code'] = ""  # No HS code provided

    if request.method == 'POST':
        # Get form data
        api_token = request.form['api_token']
        customer = customers.get(api_token)
        if not customer:
            error_message = "Invalid API token: Customer not found."
        else:
            # Consignee Address fields
            consignee_address = {
                'street_address_1': request.form['consignee_street_address_1'],
                'street_address_2': request.form['consignee_street_address_2'],
                'city': request.form['consignee_city'],
                'region': request.form['consignee_region'],
                'postal_code': request.form['consignee_postal_code'],
                'country': request.form['consignee_country']
            }
            # HS Code (optional)
            hs_code = request.form['hs_code'] if request.form['hs_code'] else None
            if hs_code:
                hs_code = hs_code.replace('.', '')  # Normalize HS code by removing dots

            # File uploads (BOL is now optional)
            bol_file_path = None
            commercial_invoice_path = None
            pga_documents = []

            if 'bol_file' in request.files:
                bol_file = request.files['bol_file']
                if bol_file and bol_file.filename and bol_file.filename.endswith('.pdf'):
                    filename = f"bol_{uuid.uuid4()}_{bol_file.filename}"
                    bol_file_path = os.path.join(UPLOAD_DIR, filename)
                    bol_file.save(bol_file_path)

            if 'commercial_invoice' in request.files:
                commercial_invoice = request.files['commercial_invoice']
                if commercial_invoice and commercial_invoice.filename and commercial_invoice.filename.endswith('.pdf'):
                    filename = f"invoice_{uuid.uuid4()}_{commercial_invoice.filename}"
                    commercial_invoice_path = os.path.join(UPLOAD_DIR, filename)
                    commercial_invoice.save(commercial_invoice_path)

            # Handle up to 5 PGA document uploads
            for i in range(1, 6):
                field_name = f'pga_document_{i}'
                if field_name in request.files:
                    pga_doc = request.files[field_name]
                    if pga_doc and pga_doc.filename and pga_doc.filename.endswith('.pdf'):
                        filename = f"pga_doc_{uuid.uuid4()}_{pga_doc.filename}"
                        pga_doc_path = os.path.join(UPLOAD_DIR, filename)
                        pga_doc.save(pga_doc_path)
                        pga_documents.append(pga_doc_path)

            shipment_data = {
                'shipper_id': request.form['shipper_id'],
                'consignee_name': request.form['consignee_name'],
                'consignee_address': consignee_address,
                'description': request.form['description'],
                'quantity': int(request.form['quantity']),
                'value': float(request.form['value']),
                'country_of_origin': request.form['country_of_origin'],
                'tracking_number': request.form['tracking_number'],
                'bol_file_path': bol_file_path,
                'commercial_invoice_path': commercial_invoice_path,
                'pga_documents': pga_documents,
                'hs_code': hs_code
            }

            # Scenario 3: Check IOR/POA status
            if not customer['is_importer_of_record'] and not customer['has_poa']:
                error_message = "Customer is not an Importer of Record and has no Power of Attorney filed on account."

            # Scenario 4: Check denied party list
            if not error_message and check_denied_party_list(shipment_data['consignee_name']):
                error_message = "Consignee is on the denied party list and fails screening."

            # Scenario 6: Check for negative value
            if not error_message and shipment_data['value'] < 0:
                error_message = "Shipment value cannot be negative."

            if not error_message:
                # Step 1: Check Type 86 eligibility
                entry_type = 'Type 86'
                fallback_reason = None
                if shipment_data['value'] > 800:
                    entry_type = 'Type 11'
                    fallback_reason = 'Value exceeds $800'
                else:
                    # Load shipments from file
                    shipments = read_shipments()
                    # Check for multiple shipments per day per consignee
                    same_day_shipments = [s for s in shipments if
                                          s['shipment_data']['consignee_name'] == shipment_data['consignee_name']]
                    if len(same_day_shipments) > 0:
                        total_value = sum(s['shipment_data']['value'] for s in same_day_shipments) + shipment_data[
                            'value']
                        if total_value > 800:
                            entry_type = 'Type 11'
                            fallback_reason = 'Multiple shipments exceed $800 aggregate value'
                            warning_message = f"Total value for consignee today ({total_value}) exceeds $800 daily limit. Fallback to Type 11."

                # Step 2: Perform HS classification (use provided HS code if available)
                if shipment_data['hs_code']:
                    hts_code = shipment_data['hs_code']
                else:
                    hts_code = mock_hs_classification(shipment_data['description'])
                    if scenario == "no_hs_code":
                        success_message = f"No HS code provided. Assigned HS code: {hts_code} by internal service."

                # Step 3: Check PGA flags using the internal service
                pga_flags, pga_full_response = lookup_pga_requirements(hts_code, shipment_data['consignee_name'],
                                                                       shipment_data['description'])
                if not pga_flags:  # Fallback to mock if service fails
                    pga_flags = mock_pga_flags(hts_code)

                if pga_flags and not warning_message:
                    warning_message = f"PGA flags triggered: {', '.join(pga_flags)}. Additional documentation may be required."

                if not error_message:
                    # Step 4: Store shipment
                    shipment_record = {
                        'shipment_data': shipment_data,
                        'entry_type': entry_type,
                        'fallback_reason': fallback_reason,
                        'hts_code': hts_code,
                        'pga_flags': pga_flags,
                        'submitted_at': datetime.utcnow().isoformat()
                    }

                    # Load existing shipments, append the new one, and save
                    shipments = read_shipments()
                    shipments.append(shipment_record)
                    write_shipments(shipments)

                    # Step 5: Return response
                    response = {
                        'status': 'success',
                        'entry_type': entry_type,
                        'fallback_reason': fallback_reason,
                        'hts_code': hts_code,
                        'pga_flags': pga_flags,
                        'pga_full_response': pga_full_response,
                        'shipment': shipment_data
                    }
                    success = True
                    if not success_message:
                        success_message = "Shipment submitted successfully."

    return render_template('shipment.html',
                           default_data=default_data,
                           error_message=error_message,
                           warning_message=warning_message,
                           success_message=success_message,
                           success=success,
                           response=response,
                           scenario=scenario)


# Define a namespace for the API
ns = api.namespace('api', description='Shipment Operations')

# Define the shipment data model for Swagger documentation
shipment_model = api.model('Shipment', {
    'shipper_id': fields.String(required=True, description='Unique identifier for the shipper'),
    'consignee_name': fields.String(required=True, description='Name of the consignee'),
    'consignee_address': fields.Nested(api.model('Address', {
        'street_address_1': fields.String(required=True),
        'street_address_2': fields.String(),
        'city': fields.String(required=True),
        'region': fields.String(required=True),
        'postal_code': fields.String(required=True),
        'country': fields.String(required=True)
    }), required=True, description='Address of the consignee'),
    'description': fields.String(required=True, description='Description of the goods'),
    'quantity': fields.Integer(required=True, description='Quantity of goods'),
    'value': fields.Float(required=True, description='Value of the shipment in USD'),
    'country_of_origin': fields.String(required=True, description='Country of origin (e.g., CN)'),
    'tracking_number': fields.String(required=True, description='Tracking number for the shipment'),
    'bol_file_path': fields.String(description='Path to uploaded BOL file'),
    'commercial_invoice_path': fields.String(description='Path to uploaded Commercial Invoice file'),
    'pga_documents': fields.List(fields.String, description='List of uploaded PGA documents'),
    'hs_code': fields.String(description='Optional 10-digit HS code')
})

# Define the response model for Swagger documentation
response_model = api.model('ShipmentResponse', {
    'status': fields.String(description='Status of the request'),
    'entry_type': fields.String(description='Entry type (Type 86 or Type 11)'),
    'fallback_reason': fields.String(description='Reason for fallback, if applicable'),
    'hts_code': fields.String(description='Harmonized Tariff Schedule code'),
    'pga_flags': fields.List(fields.String, description='List of PGA flags triggered'),
    'pga_full_response': fields.Raw(description='Full response from PGA API'),
    'shipment': fields.Nested(shipment_model, description='Submitted shipment data')
})


# API endpoint for shipment submission with Swagger documentation
@ns.route('/submit-shipment')
class ShipmentResource(Resource):
    @ns.doc('submit_shipment', security='apikey')
    @ns.expect(shipment_model)
    @ns.response(200, 'Success', response_model)
    @ns.response(400, 'Bad Request')
    @ns.response(401, 'Unauthorized')
    def post(self):
        """Submit a shipment for Type 86 processing with fallback to Type 11"""
        api_token = request.headers.get('Authorization')
        if not api_token:
            return {'error': 'API token required'}, 401

        shipment_data = request.json
        if not shipment_data:
            return {'error': 'Shipment data required'}, 400

        response, status = submit_shipment(api_token, shipment_data)
        return response, status


# Define security for Swagger (API token in header)
api.security = [{
    'apikey': {
        'type': 'apiKey',
        'name': 'Authorization',
        'in': 'header'
    }
}]

if __name__ == '__main__':
    app.run(debug=True)