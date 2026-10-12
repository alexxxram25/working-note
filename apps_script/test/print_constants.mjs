// Prints Code.gs's sheet constants as JSON, so the Python suite can hold them
// to backend/coach/sheet_schema.py without parsing JavaScript itself.
import { loadCoach } from './gas_mocks.mjs';

const { constants } = loadCoach();
process.stdout.write(JSON.stringify(constants));
