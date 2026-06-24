import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import threading
from app.core.config import settings

def send_email_async(to_email: str, subject: str, html_content: str):
    """Run SMTP sending in a background thread."""
    
    def send():
        if not settings.SMTP_USERNAME or not settings.SMTP_PASSWORD:
            print("Email service disabled: SMTP credentials not configured.")
            return

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = settings.SMTP_FROM_EMAIL
            msg["To"] = to_email

            part = MIMEText(html_content, "html")
            msg.attach(part)

            with smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT) as server:
                server.starttls()
                server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                server.send_message(msg)
            print(f"Large transaction alert sent to {to_email}")
        except Exception as e:
            print(f"Failed to send email: {e}")

    thread = threading.Thread(target=send)
    thread.daemon = True
    thread.start()

# Add these helper functions above the EmailService class

def _format_category(value: str) -> str:
    """Converts 'Category.other' → 'Other'"""
    if "." in value:
        value = value.split(".")[-1]
    return value.replace("_", " ").title()

def _format_type(value: str) -> str:
    """Converts 'TransactionType.credit' → 'Credit'"""
    if "." in value:
        value = value.split(".")[-1]
    return value.replace("_", " ").title()


class EmailService:
    @staticmethod
    def send_large_transaction_alert(user, transaction):
        """Sends a beautiful HTML email for transactions >= 10000"""

        is_credit = transaction.type.split(".")[-1].lower() == "credit" if "." in str(transaction.type) else transaction.type == "credit"
        color = "#10B981" if is_credit else "#EF4444"
        color_bg = "rgba(16,185,129,0.15)" if is_credit else "rgba(239,68,68,0.15)"
        verb = "Received" if is_credit else "Spent"
        sign = "+" if is_credit else "-"
        credited_label = "Credited to" if is_credit else "Debited from"

        # Clean enum strings
        category_display = _format_category(str(transaction.category))
        type_display = _format_type(str(transaction.type))

        # Indian Rupee format
        amount_str = f"₹{transaction.amount:,.2f}"

        # Optional date formatting
        date_str = "Just now"
        if hasattr(transaction, "date") and transaction.date:
            try:
                date_str = transaction.date.strftime("%d %b %Y, %I:%M %p")
            except Exception:
                pass

        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 24px 12px; background-color: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; color: #f8fafc;">

    <div style="max-width: 560px; margin: 0 auto; border-radius: 20px; overflow: hidden;">

        <!-- Header -->
        <div style="background-color: {color}; padding: 32px 24px 24px; text-align: center;">
            <div style="display: inline-block; background: rgba(255,255,255,0.15); border: 1px solid rgba(255,255,255,0.25); border-radius: 100px; padding: 5px 14px; font-size: 12px; color: rgba(255,255,255,0.95); font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 14px;">
                &#9679; &nbsp;Transaction Alert
            </div>
            <h1 style="margin: 0 0 6px; color: #ffffff; font-size: 22px; font-weight: 800; letter-spacing: -0.03em;">Large Transaction Detected</h1>
            <p style="margin: 0; color: rgba(255,255,255,0.85); font-size: 13px;">A significant transaction was recorded on your account.</p>
        </div>

        <!-- Body -->
        <div style="background-color: #1e293b; padding: 28px 20px;">

            <p style="font-size: 15px; color: #cbd5e1; margin: 0 0 6px;">Hi {user.name},</p>
            <p style="font-size: 13px; color: #64748b; margin: 0 0 24px; line-height: 1.6;">
                We noticed a large transaction on your FinTrack account. Please review the details below and confirm it was authorized by you.
            </p>

            <!-- Amount Card -->
            <div style="border-radius: 14px; background: #0f172a; border: 1px solid rgba(255,255,255,0.06); padding: 24px 16px; text-align: center; margin-bottom: 24px;">
                <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: #64748b; font-weight: 700; margin-bottom: 8px;">Amount {verb}</div>
                <div style="font-size: 36px; font-weight: 800; color: {color}; letter-spacing: -0.03em; line-height: 1;">{sign}{amount_str}</div>
                <div style="margin-top: 8px; font-size: 12px; color: #475569;">{credited_label} your account</div>
            </div>

            <!-- Details Section Label -->
            <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: #475569; font-weight: 700; margin: 0 0 10px;">Transaction Details</div>

            <!-- Details Box -->
            <div style="border-radius: 12px; background: #0f172a; border: 1px solid rgba(255,255,255,0.05); overflow: hidden; margin-bottom: 20px;">

                <!-- Transaction ID: stacked layout for long values -->
                <div style="padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.04);">
                    <div style="font-size: 12px; color: #64748b; margin-bottom: 4px;">&#128196; &nbsp;Transaction ID</div>
                    <div style="font-size: 11px; color: #e2e8f0; font-weight: 500; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; word-break: break-all;">{transaction.id}</div>
                </div>

                <!-- Description -->
                <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.04);">
                    <span style="font-size: 13px; color: #64748b; white-space: nowrap;">&#128221; &nbsp;Description</span>
                    <span style="font-size: 13px; color: #e2e8f0; font-weight: 500; text-align: right;">{transaction.description}</span>
                </div>

                <!-- Category -->
                <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.04);">
                    <span style="font-size: 13px; color: #64748b; white-space: nowrap;">&#127991; &nbsp;Category</span>
                    <span style="font-size: 13px; color: #e2e8f0; font-weight: 500; text-align: right;">{category_display}</span>
                </div>

                <!-- Date -->
                <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.04);">
                    <span style="font-size: 13px; color: #64748b; white-space: nowrap;">&#128197; &nbsp;Date</span>
                    <span style="font-size: 13px; color: #e2e8f0; font-weight: 500; text-align: right;">{date_str}</span>
                </div>

                <!-- Type -->
                <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px;">
                    <span style="font-size: 13px; color: #64748b; white-space: nowrap;">&#128179; &nbsp;Type</span>
                    <span style="background: {color_bg}; color: {color}; border-radius: 6px; padding: 4px 12px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; white-space: nowrap;">{type_display}</span>
                </div>

            </div>

            <!-- Warning Box -->
            <div style="border-radius: 10px; background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.2); padding: 14px 16px;">
                <p style="font-size: 12px; color: #94a3b8; line-height: 1.6; margin: 0;">
                    <strong style="color: #fca5a5; font-weight: 600;">&#9888;&#65039; Didn't authorize this?</strong><br>
                    If you don't recognize this transaction, please review your account immediately and contact our support team.
                </p>
            </div>

        </div>

        <!-- Footer -->
        <div style="background-color: #0f172a; padding: 20px 24px; text-align: center; border-top: 1px solid rgba(255,255,255,0.04);">
            <p style="font-size: 13px; font-weight: 700; color: #475569; letter-spacing: 0.04em; margin: 0 0 4px;">FINTRACK</p>
            <p style="font-size: 11px; color: #334155; margin: 0 0 12px;">&#169; 2026 FinTrack. All rights reserved.</p>
            <div style="display: flex; justify-content: center; gap: 20px;">
                <a href="#" style="font-size: 11px; color: #475569; text-decoration: none;">Unsubscribe</a>
                <a href="#" style="font-size: 11px; color: #475569; text-decoration: none;">Privacy Policy</a>
                <a href="#" style="font-size: 11px; color: #475569; text-decoration: none;">Support</a>
            </div>
        </div>

    </div>

</body>
</html>"""

        subject = f"Alert: Large {verb} of {amount_str}"
        send_email_async(user.email, subject, html_content)# Add these helper functions above the EmailService class

def _format_category(value: str) -> str:
    """Converts 'Category.other' → 'Other'"""
    if "." in value:
        value = value.split(".")[-1]
    return value.replace("_", " ").title()

def _format_type(value: str) -> str:
    """Converts 'TransactionType.credit' → 'Credit'"""
    if "." in value:
        value = value.split(".")[-1]
    return value.replace("_", " ").title()


class EmailService:
    @staticmethod
    def send_large_transaction_alert(user, transaction):
        """Sends a beautiful HTML email for transactions >= 10000"""

        is_credit = transaction.type.split(".")[-1].lower() == "credit" if "." in str(transaction.type) else transaction.type == "credit"
        color = "#10B981" if is_credit else "#EF4444"
        color_bg = "rgba(16,185,129,0.15)" if is_credit else "rgba(239,68,68,0.15)"
        verb = "Received" if is_credit else "Spent"
        sign = "+" if is_credit else "-"
        credited_label = "Credited to" if is_credit else "Debited from"

        # Clean enum strings
        category_display = _format_category(str(transaction.category))
        type_display = _format_type(str(transaction.type))

        # Indian Rupee format
        amount_str = f"₹{transaction.amount:,.2f}"

        # Optional date formatting
        date_str = "Just now"
        if hasattr(transaction, "date") and transaction.date:
            try:
                date_str = transaction.date.strftime("%d %b %Y, %I:%M %p")
            except Exception:
                pass

        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin: 0; padding: 24px 12px; background-color: #0f172a; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; color: #f8fafc;">

    <div style="max-width: 560px; margin: 0 auto; border-radius: 20px; overflow: hidden;">

        <!-- Header -->
        <div style="background-color: {color}; padding: 32px 24px 24px; text-align: center;">
            <div style="display: inline-block; background: rgba(255,255,255,0.15); border: 1px solid rgba(255,255,255,0.25); border-radius: 100px; padding: 5px 14px; font-size: 12px; color: rgba(255,255,255,0.95); font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 14px;">
                &#9679; &nbsp;Transaction Alert
            </div>
            <h1 style="margin: 0 0 6px; color: #ffffff; font-size: 22px; font-weight: 800; letter-spacing: -0.03em;">Large Transaction Detected</h1>
            <p style="margin: 0; color: rgba(255,255,255,0.85); font-size: 13px;">A significant transaction was recorded on your account.</p>
        </div>

        <!-- Body -->
        <div style="background-color: #1e293b; padding: 28px 20px;">

            <p style="font-size: 15px; color: #cbd5e1; margin: 0 0 6px;">Hi {user.name},</p>
            <p style="font-size: 13px; color: #64748b; margin: 0 0 24px; line-height: 1.6;">
                We noticed a large transaction on your FinTrack account. Please review the details below and confirm it was authorized by you.
            </p>

            <!-- Amount Card -->
            <div style="border-radius: 14px; background: #0f172a; border: 1px solid rgba(255,255,255,0.06); padding: 24px 16px; text-align: center; margin-bottom: 24px;">
                <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; color: #64748b; font-weight: 700; margin-bottom: 8px;">Amount {verb}</div>
                <div style="font-size: 36px; font-weight: 800; color: {color}; letter-spacing: -0.03em; line-height: 1;">{sign}{amount_str}</div>
                <div style="margin-top: 8px; font-size: 12px; color: #475569;">{credited_label} your account</div>
            </div>

            <!-- Details Section Label -->
            <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: #475569; font-weight: 700; margin: 0 0 10px;">Transaction Details</div>

            <!-- Details Box -->
            <div style="border-radius: 12px; background: #0f172a; border: 1px solid rgba(255,255,255,0.05); overflow: hidden; margin-bottom: 20px;">

                <!-- Transaction ID: stacked layout for long values -->
                <div style="padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.04);">
                    <div style="font-size: 12px; color: #64748b; margin-bottom: 4px;">&#128196; &nbsp;Transaction ID</div>
                    <div style="font-size: 11px; color: #e2e8f0; font-weight: 500; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; word-break: break-all;">{transaction.id}</div>
                </div>

                <!-- Description -->
                <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.04);">
                    <span style="font-size: 13px; color: #64748b; white-space: nowrap;">&#128221; &nbsp;Description</span>
                    <span style="font-size: 13px; color: #e2e8f0; font-weight: 500; text-align: right;">{transaction.description}</span>
                </div>

                <!-- Category -->
                <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.04);">
                    <span style="font-size: 13px; color: #64748b; white-space: nowrap;">&#127991; &nbsp;Category</span>
                    <span style="font-size: 13px; color: #e2e8f0; font-weight: 500; text-align: right;">{category_display}</span>
                </div>

                <!-- Date -->
                <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid rgba(255,255,255,0.04);">
                    <span style="font-size: 13px; color: #64748b; white-space: nowrap;">&#128197; &nbsp;Date</span>
                    <span style="font-size: 13px; color: #e2e8f0; font-weight: 500; text-align: right;">{date_str}</span>
                </div>

                <!-- Type -->
                <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px;">
                    <span style="font-size: 13px; color: #64748b; white-space: nowrap;">&#128179; &nbsp;Type</span>
                    <span style="background: {color_bg}; color: {color}; border-radius: 6px; padding: 4px 12px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; white-space: nowrap;">{type_display}</span>
                </div>

            </div>

            <!-- Warning Box -->
            <div style="border-radius: 10px; background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.2); padding: 14px 16px;">
                <p style="font-size: 12px; color: #94a3b8; line-height: 1.6; margin: 0;">
                    <strong style="color: #fca5a5; font-weight: 600;">&#9888;&#65039; Didn't authorize this?</strong><br>
                    If you don't recognize this transaction, please review your account immediately and contact our support team.
                </p>
            </div>

        </div>

        <!-- Footer -->
        <div style="background-color: #0f172a; padding: 20px 24px; text-align: center; border-top: 1px solid rgba(255,255,255,0.04);">
            <p style="font-size: 13px; font-weight: 700; color: #475569; letter-spacing: 0.04em; margin: 0 0 4px;">FINTRACK</p>
            <p style="font-size: 11px; color: #334155; margin: 0 0 12px;">&#169; 2026 FinTrack. All rights reserved.</p>
            <div style="display: flex; justify-content: center; gap: 20px;">
                <a href="#" style="font-size: 11px; color: #475569; text-decoration: none;">Unsubscribe</a>
                <a href="#" style="font-size: 11px; color: #475569; text-decoration: none;">Privacy Policy</a>
                <a href="#" style="font-size: 11px; color: #475569; text-decoration: none;">Support</a>
            </div>
        </div>

    </div>

</body>
</html>"""

        subject = f"Alert: Large {verb} of {amount_str}"
        send_email_async(user.email, subject, html_content)