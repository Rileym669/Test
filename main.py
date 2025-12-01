from flask import Flask, request, jsonify, make_response
from flask_cors import CORS, cross_origin
import base64, psycopg2, requests, os
from datetime import datetime

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

FHIR_BASE_URL = 'https://hapi.fhir.org/baseR4'

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/", methods=["GET"])
def root():
    return "Service running", 200

@app.route('/create', methods=['POST', 'OPTIONS'])
@cross_origin(origins="*")
def create():
    if request.method == "OPTIONS":
        return make_response("", 204)

    data = request.get_json()
    name = data.get('name')
    gender = data.get('gender')
    birthDate = data.get('birthDate')

    patient_resource = {
        'resourceType': 'Patient',
        'name': [{'text': name}],
        "gender": gender,
        "birthDate": birthDate
    }

    response = requests.post(
        f"{FHIR_BASE_URL}/Patient",
        headers={"Content-Type": "application/fhir+json"},
        json=patient_resource
    )

    if response.status_code in (200, 201):
        return jsonify({"message": "Patient created", "fhir_response": response.json()}), 201
    else:
        return jsonify({"message": "Failed", "error": response.text}), response.status_code


# --- DATABASE CONFIG ---
DB_CONFIG = {
    'host': 'pneumonia-predictor-db.c9aq6omq2ivs.us-east-2.rds.amazonaws.com',
    'database': 'postgres',
    'user': 'gatechAdmin',
    'password': 'fepnab-hEzxa3-rovcyn',
    'port': 5432
}

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)

def get_patient_image_by_fields(first_name, last_name, gender, birth_date):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT data FROM patient_xray_images
        WHERE first_name=%s AND last_name=%s AND gender=%s AND birth_date=%s
        LIMIT 1
    """, (first_name, last_name, gender.lower(), birth_date))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


@app.route('/search', methods=['GET', 'OPTIONS'])
@cross_origin(origins="*")
def search():
    if request.method == "OPTIONS":
        return make_response("", 204)

    name = request.args.get('name')
    last_name = request.args.get('lastName')
    birthDate = request.args.get('birthDate')

    params = {"_count": 10}
    if name: params["name"] = name
    if last_name: params["family"] = last_name
    if birthDate:
        try:
            dt = datetime.strptime(birthDate, "%m/%d/%Y")
            params["birthdate"] = dt.strftime("%Y-%m-%d")
        except ValueError:
            return jsonify({"message": "Bad date format (MM/DD/YYYY expected)"}), 400

    try:
        response = requests.get(f"{FHIR_BASE_URL}/Patient", params=params)
        response.raise_for_status()
        bundle = response.json()

        patients = []

        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})

            first_name = ""
            last_name = ""
            if resource.get("name"):
                name_obj = resource["name"][0]
                if name_obj.get("given"):
                    first_name = name_obj["given"][0]
                last_name = name_obj.get("family", "")

            birth_date = resource.get("birthDate", "")
            gender = resource.get("gender", "")
            patient_id = resource.get("id", "")

            image_data = None
            if first_name and last_name and birth_date and gender:
                image_data = get_patient_image_by_fields(first_name, last_name, gender, birth_date)

            image_base64 = base64.b64encode(image_data).decode() if image_data else None

            patients.append({
                "patientId": patient_id,
                "firstName": first_name or "N/A",
                "lastName": last_name or "N/A",
                "birthDate": birth_date or "N/A",
                "sex": gender.capitalize() if gender else "N/A",
                "xrayImage": image_base64
            })

        return jsonify(patients)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/analyze', methods=['POST', 'OPTIONS'])
@cross_origin(origins="*")
def analyze_patient():
    if request.method == "OPTIONS":
        return make_response("", 204)

    data = request.get_json()
    patient_id = data.get("patient_id")
    first_name = data.get("first_name")
    last_name = data.get("last_name")
    birth_date = data.get("birthDate")
    gender = data.get("sex")

    if not (first_name and last_name and birth_date and gender and patient_id):
        return jsonify({"error": "Missing required patient fields"}), 400

    # Get the X-ray image from database
    image_data = get_patient_image_by_fields(first_name, last_name, gender.lower(), birth_date)
    if not image_data:
        return jsonify({"error": "No X-ray image found for this patient"}), 404

    # Convert to Base64 string
    image_base64 = base64.b64encode(image_data).decode("utf-8")

    # Prepares request for AI endpoint
    analysis_request = {
        "patient_id": patient_id,
        "patient_first_name": first_name,
        "image": image_base64
    }

    try:
        response = requests.post(ANALYSIS_APP_URL, json=analysis_request, headers={"Content-Type": "application/json"})
        response.raise_for_status()
        return jsonify(response.json())
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"AI analysis request failed: {str(e)}"}), 500



