from flask import Flask, request, jsonify, make_response
import base64, psycopg2, requests
from flask_cors import CORS  # Allow React app to call Flask
from datetime import datetime

app = Flask(__name__)
CORS(app) # Enable Cross-Origin Resource Sharing

# @app.after_request
# def add_cors_headers(response):
#     response.headers["Access-Control-Allow-Origin"] = "*"  # or specific domain
#     response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
#     response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
#     return response

FHIR_BASE_URL = 'https://hapi.fhir.org/baseR4'

ANALYSIS_APP_URL = "http://3.93.240.12:5000/api/predictNeumonia"

DB_CONFIG = {
    'host': 'pneumonia-predictor-db.c9aq6omq2ivs.us-east-2.rds.amazonaws.com',
    'database': 'postgres',
    'user': '<REDACTED>',
    'password': '<REDACTED>',
    'port': 5432
}

def get_db_connection():
    return psycopg2.connect(**DB_CONFIG)

# Retrieves a patient's x-ray image from the database using first+last+gender+birthDate.
# Returns binary data if found, else None.
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

@app.route('/create', methods=['POST', 'OPTIONS'])
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
        return jsonify({
            "message": "Patient created successfully",
            "fhir_response": response.json()
        }), 201
    else:
        return jsonify({
            "message": "Failed to create patient",
            "error": response.text
        }), response.status_code


@app.route('/search', methods=['GET', 'OPTIONS'])
def search():
    if request.method == "OPTIONS":
        return make_response("", 204)

    name = request.args.get('name')
    last_name = request.args.get('lastName')
    birthDate = request.args.get('birthDate')

    # Build query parameters dynamically
    params = {"_count": 10}
    if name:
        params["name"] = name
    if last_name:
        params["family"] = last_name
    if birthDate:
        try:
            dt = datetime.strptime(birthDate, "%m/%d/%Y")
            params["birthdate"] = dt.strftime("%Y-%m-%d")
        except ValueError:
            return jsonify({"message": "Invalid birthDate format, expected MM/DD/YYYY"}), 400

    try:
        response = requests.get(f"{FHIR_BASE_URL}/Patient", params=params)
        response.raise_for_status()
        bundle = response.json()
        patients = []

        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})
            patient_id = resource.get("id", "")
            
            # Extract first and last names safely
            first_name = ""
            last_name = ""
            if "name" in resource and len(resource["name"]) > 0:
                name_obj = resource["name"][0]
                if "given" in name_obj and len(name_obj["given"]) > 0:
                    first_name = name_obj["given"][0]
                last_name = name_obj.get("family", "")

            birth_date = resource.get("birthDate", "")
            sex = resource.get("gender", "")

            # Queries the database for an X-ray image if we all required fields are present
            image_data = None
            if first_name != "N/A" and last_name != "N/A" and birth_date != "N/A" and sex:
                image_data = get_patient_image_by_fields(first_name, last_name, sex.lower(), birth_date)

            # If image exists, convert to base64 string to send to frontend
            image_base64 = None
            if image_data:
                image_base64 = base64.b64encode(image_data).decode('utf-8')

            patients.append({
                "patientId": patient_id,
                "firstName": first_name or "N/A",
                "lastName": last_name or "N/A",
                "birthDate": birth_date or "N/A",
                "sex": sex.capitalize() if sex else "N/A",
                "xrayImage": image_base64
            })

        return jsonify(patients)

    except requests.exceptions.RequestException as e:
        return jsonify({
            "message": "Patient search failed",
            "error": str(e)
        }), 500


# Retrieves the X-ray image from DB and calls the AI prediction endpoint.
@app.route('/analyze', methods=['POST'])
def analyze_patient():
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

# def app_entry_point(request):
#     return app(request)



""" Sample Javascript
const searchPatients = async (name, birthDate) => {
  const query = new URLSearchParams();
  if (name) query.append("name", name);
  if (birthDate) query.append("birthDate", birthDate);

  const res = await fetch(`http://localhost:5000/search?${query.toString()}`);
  const data = await res.json();
  console.log(data);
};
"""
