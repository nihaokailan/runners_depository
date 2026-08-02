import os
import unittest
from unittest.mock import patch

import app


class RegistrationEmailTests(unittest.TestCase):
    def test_persist_receipt_file_falls_back_to_local_storage(self):
        receipt_path = os.path.join(app.UPLOAD_DIR, "receipt_test.png")
        with open(receipt_path, "wb") as handler:
            handler.write(b"fake-image")
        try:
            with patch.object(app, "IS_VERCEL", True), patch.object(app, "BLOB_READ_WRITE_TOKEN", ""):
                stored_name, error = app.persist_receipt_file("receipt_test.png")
            self.assertEqual(stored_name, "receipt_test.png")
            self.assertEqual(error, "")
        finally:
            if os.path.exists(receipt_path):
                os.remove(receipt_path)

    def test_verified_status_triggers_confirmation_email(self):
        runner = {
            "email": "runner@example.com",
            "first_name": "Juan",
            "middle_name": "Santos",
            "surname": "Dela Cruz",
            "payment_mode": "GCash",
            "payment_date": "2026-08-02",
            "shirt_size": "M",
            "years_running": 3,
        }

        with patch("app.smtplib.SMTP") as smtp_cls:
            smtp_instance = smtp_cls.return_value.__enter__.return_value
            sent, message = app.send_registration_confirmation_email(runner)

            self.assertTrue(sent)
            self.assertIn("sent", message.lower())
            smtp_instance.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
