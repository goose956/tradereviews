# GafferApp — App Knowledge Base

> **Last updated: March 2026**
> This document is used by the AI help assistant to answer tradesperson questions.
> Keep it up to date whenever features are added or changed.

---

## What is GafferApp?

GafferApp is a WhatsApp-based SaaS platform built for UK tradespeople (plumbers, electricians, builders, roofers, landscapers, etc.). It helps them:

- Collect Google reviews from customers via WhatsApp
- Auto-reply to Google reviews using AI
- Send invoices and quotes as professional PDFs via WhatsApp
- Track income and manage their business accounts
- Run marketing campaigns to opted-in customers
- Chat with customers directly through WhatsApp

Everything is controlled through WhatsApp commands — no separate app needed. There's also a web dashboard for settings, invoices, accounts, and more.

---

## WhatsApp Commands

All commands are sent via WhatsApp to the GafferApp number.

### /SETUP <Name> <Phone>
Add a new customer and send them a welcome/icebreaker message.
- Example: `/SETUP John Smith 07845774563`
- This also sets them as your "active customer" for chat and invoicing.

### /REVIEW [Name Phone]
Send a review request to a customer.
- With no arguments: sends to your active customer
- With arguments: `/REVIEW Jane Doe 07911223344`
- The customer receives a friendly WhatsApp message with a button. If they tap "Great", they get your Google review link. If they say things weren't great, you're notified privately.

### /INVOICE [Amount] [Description]
Create and send a professional invoice to your active customer.
- Full command: `/INVOICE 250 Bathroom refit` — creates the invoice immediately
- Partial command: `/INVOICE 250` — the bot will ask what it's for
- No arguments: `/INVOICE` — the bot walks you through it step by step, asking for amount and description
- The invoice is saved in the database with a proper invoice number, VAT calculation, and line item
- You can view, edit, or download the PDF from the web dashboard (Invoices tab)
- Type **CANCEL** at any time during the step-by-step process to abort
- For detailed invoices with multiple line items, tax settings, and payment terms, use the web dashboard

### /QUOTE [Amount] [Description]
Create and send a professional quote to your active customer.
- Works exactly like `/INVOICE` — full, partial, or guided step-by-step
- Full command: `/QUOTE 450 Full bathroom refit` — creates the quote immediately
- Partial command: `/QUOTE 450` — the bot will ask what it's for
- No arguments: `/QUOTE` — guided step-by-step flow
- Quotes are valid for 30 days by default
- The quote is saved in the database with a quote number, VAT calculation, and line item
- You can view, edit, or download the PDF from the web dashboard (Quotes tab)
- Type **CANCEL** at any time during the step-by-step process to abort

### /CHAT <Name or Phone>
Switch your active chat to a different customer.
- Example: `/CHAT John` or `/CHAT 07845774563`
- After switching, any normal text you type gets relayed to that customer with your business name attached.
- You must have an active customer set before using /INVOICE, /QUOTE, or /REVIEW (without args)

### /LOGIN
Get a one-time login code sent to your WhatsApp, along with a link to your web dashboard. Enter the code on the login page to access your account. Codes expire in 5 minutes.

### /HELP
- With no arguments: shows a quick list of all commands
- With a question: `/HELP how do I send an invoice?` — asks the AI assistant for a detailed answer about anything in the app

### Normal text (no slash)
If you type a normal message (without a `/` at the start), it gets relayed to your active customer. The message appears to come from your business name.

---

## Web Dashboard (Portal)

Access via `/LOGIN` or by going to your dashboard URL and entering your phone + OTP code.

### Tabs

**Settings**
- Business info: name, owner name, trade type, phone, email, Google review link
- Auto-reply: enable/disable AI-powered auto-replies to Google reviews, set star threshold, customise positive/negative reply templates
- Follow-ups: enable automatic follow-up messages for customers who haven't responded to review requests (up to 3 messages, configurable timing)
- Business address: street, city, postcode, country
- Tax & invoicing: tax label (VAT/GST/Sales Tax), tax number, tax rate, currency (GBP default), default payment terms, bank details
- Payment methods: choose which you accept (cash, bank transfer, card, PayPal, Stripe, other). Set an online payment link (e.g. PayPal.me) — it's auto-sent with invoices.

**Customers**
- List of all your customers with name, phone, status, follow-up count, marketing opt-in, and request date

**Messages**
- Full log of all outbound messages: date, recipient, type, message body, status

**Campaigns**
- Send promotional messages to all customers who opted in to marketing
- Daily limit: 50 messages
- Campaign history with sent/failed counts and status

**Invoices**
- Create invoices with line items, customer selection, due date, payment terms, and notes
- Tax is auto-calculated from your settings
- Actions: download PDF, send via WhatsApp (sends PDF + payment link), mark as paid (select payment method)
- Invoice statuses: draft, sent, paid, overdue, cancelled

**Quotes**
- Create quotes with line items, customer, valid-until date, and notes
- Download as PDF or send via WhatsApp
- Quote statuses: draft, sent, accepted, declined, expired

**Accounts**
- Summary cards: Total Income (paid), Outstanding Balance, This Month's Income
- Monthly breakdown table showing paid vs outstanding per month
- Full invoice list with download PDF, mark as paid, and delete actions

**Drafts**
- AI-generated reply drafts for Google reviews
- Review and approve/reject before posting

---

## Review Request Flow

1. Tradesperson sends `/REVIEW John 07911123456` (or uses active customer)
2. Customer receives a WhatsApp message: "Hi John, how was your experience with [Business]?"
3. Customer taps "Great" → gets the Google review link
4. Customer leaves a review → Google review appears
5. If auto-reply is enabled, AI drafts a reply which the tradesperson can approve/edit/reject
6. If the customer didn't respond, automatic follow-ups are sent (if enabled)

---

## Invoicing & Payments

- Quick invoices via `/INVOICE 250 Boiler service` in WhatsApp (amount + description)
- If you leave out info, the bot asks follow-up questions to fill in the gaps
- Just `/INVOICE` with no arguments starts a guided step-by-step flow
- Type **CANCEL** at any point during the guided flow to abort
- Detailed invoices with multiple line items: use the web dashboard
- PDFs generated automatically — professional layout with business details, line items, totals, payment terms, bank details
- Send invoice PDF via WhatsApp to the customer
- If you have a PayPal.me link configured, the invoice amount is auto-appended and sent as a clickable payment link
- Mark invoices as paid manually (select payment method: cash, bank transfer, card, PayPal, Stripe, other)
- All paid invoices contribute to your income tracking in the Accounts tab

---

## Authentication / Login

- No passwords or Google sign-in needed
- Log in via WhatsApp: send `/LOGIN` or go to the login page
- Enter your registered phone number → receive a 6-digit code on WhatsApp
- Enter the code → you're logged in for 30 days
- Secure: codes expire in 5 minutes, rate-limited to 3 attempts per 5 minutes

---

## Marketing Campaigns

- Customers can opt in to receive promotional messages
- Send campaigns from the dashboard Campaigns tab
- Daily sending limit: 50 messages per day
- Campaign history tracks sent, failed, and status

---

## Frequently Asked Questions

**How do I get started?**
Message the GafferApp WhatsApp number. You'll be guided through setup. Use `/SETUP` to add your first customer.

**How do I change my business details?**
Send `/LOGIN` to get access to your web dashboard, then go to the Settings tab.

**How do I send an invoice?**
Quick way: `/INVOICE 250 Bathroom refit` — creates and sends immediately.
Guided way: Just type `/INVOICE` and the bot asks you for the amount and description step by step.
Detailed way: Log into your dashboard → Invoices tab → fill in line items, customer, terms → Create Invoice → click "Send" to WhatsApp the PDF.

**What if I forget to include details in a command?**
The bot will ask you for the missing info. For example, `/INVOICE` or `/QUOTE` without an amount will prompt you to enter one. You can type CANCEL at any time to abort.

**How do I send a quote?**
Quick way: `/QUOTE 450 Full bathroom refit` — creates and sends immediately.
Guided way: Just type `/QUOTE` and the bot asks for the amount and description step by step.
Detailed way: Log into your dashboard → Quotes tab → fill in line items, customer, valid-until date → Create Quote → click "Send" to WhatsApp the PDF.
Quotes are valid for 30 days by default.

**How do I track my income?**
Log into your dashboard → Accounts tab. You'll see total paid income, outstanding balance, this month's earnings, and a monthly breakdown.

**How do I mark an invoice as paid?**
In the dashboard (Invoices or Accounts tab), click the "✅ Paid" button on any unpaid invoice and select the payment method.

**Can customers pay online?**
Set up a PayPal.me or Stripe payment link in Settings → Tax & Invoicing → Online Payment Link. When you send an invoice via WhatsApp, the payment link is automatically included with the invoice amount.

**How do follow-ups work?**
If enabled in Settings, customers who don't respond to a review request get automatic follow-up messages. You can set the timing (1-7 days) and up to 3 follow-up messages.

**How does the AI auto-reply work?**
When a customer leaves a Google review, AI drafts a professional reply. If the review is above your star threshold, it can auto-post. Otherwise, you review and approve it first via WhatsApp or the Drafts tab.

**Is my data secure?**
Yes. Authentication is via WhatsApp OTP codes. Sessions expire after 30 days. Sensitive tokens are encrypted. No passwords are stored.

**What trades are supported?**
Electricians, plumbers, builders, roofers, landscapers, and more. Select your trade in Settings or choose "Other".

**What currencies are supported?**
GBP (default), USD, EUR, AUD, CAD, NZD, ZAR. Set in Settings → Tax & Invoicing.

---

## Error Handling & Validation

The bot validates all commands and asks for more information when something is missing:

- **Missing args**: Commands like `/INVOICE` or `/QUOTE` without details trigger a guided step-by-step flow asking for each piece of info
- **Bad input**: If you enter text where a number is expected (e.g. for the amount), the bot asks again
- **No active customer**: `/INVOICE`, `/QUOTE`, and `/REVIEW` (without args) require an active customer — the bot tells you to use `/SETUP` or `/CHAT` first
- **Invalid phone numbers**: `/SETUP` and `/REVIEW` validate the phone/name format
- **Cancel anytime**: Type **CANCEL** during any guided flow to abort
- **New command clears pending**: Starting a new `/` command automatically cancels any in-progress guided flow
- **Unknown command**: Any `/` command not recognised shows a helpful error pointing to `/HELP`
