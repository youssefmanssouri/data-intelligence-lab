# Security Policy: Data Intelligence Lab

**Project Status:** Pre-Implementation Phase  
**Current Baseline:** Design Specification Only — No Controls Empirically Verified  

---

## 1. Reporting Security Vulnerabilities

Data Intelligence Lab takes security seriously. If you believe you have found a security vulnerability or design flaw in this repository, please report it responsibly:

- **Preferred Method:** Open a private draft security advisory via **GitHub Security Advisories** on the repository page (once published).
- **Alternative Method:** If GitHub Security Advisories are not yet configured on your fork/clone, contact the project maintainer via the profile contact options on GitHub or open an issue marked with the `security` label (ensuring sensitive exploit payloads or private data are omitted).

Please do not disclose security vulnerabilities publicly until an initial assessment has been completed.

---

## 2. Security Status: Pre-Implementation Notice

This project is currently in the **foundation documentation phase**. 

- **No Application Code:** No API endpoints, database engines, or UI components are yet active.
- **Unverified Controls:** Architectural security controls described in the documentation (such as input sanitization, memory limits, and query timeouts) represent **target design constraints**. They have not yet been implemented or empirically tested.
- **No Compliance Claim:** Alignment with security standards (such as OWASP ASVS) represents an engineering target, not an audit certification or claim of achieved compliance.

---

## 3. Intended Security Architecture & Baseline

The platform's long-term security architecture is documented in [`docs/02-architecture.md`](docs/02-architecture.md). It is governed by the following core principles:

### 3.1 Local-First Privacy Model (V1 Target)
- **Localhost Binding:** In local mode, the backend server must bind strictly to the loopback interface (`127.0.0.1`). Binding to public network interfaces (`0.0.0.0`) is prohibited.
- **Zero Remote Telemetry:** No user dataset contents, metadata, or diagnostic samples may be transmitted over the network by default.
- **Data Isolation:** User uploads must be stored exclusively on the host filesystem under `~/.data-intelligence-lab/storage/`, isolated by random UUID identifiers.

### 3.2 Target Baseline: OWASP ASVS 5.0.0 Level 2
The application targets the **OWASP Application Security Verification Standard (ASVS) 5.0.0 Level 2** baseline for applications handling business data:
- **V1 Architecture & Threat Modeling:** Trust boundaries and STRIDE threat matrices are formally documented.
- **V5 Input Validation (Critical):** All incoming CSV streams and query strings are treated as untrusted input, with strict cell-size limits (32 KB) and file-size ceilings (200 MB).
- **V8 Data Protection:** Isolation of temporary quarantine uploads from permanent Parquet files.
- **V14 Configuration Hardening:** Target configuration mandates disabling DuckDB external filesystem and network access (`enable_external_access = false`).

*Justified Local Mode Exceptions:* Requirements for multi-user authentication (V2), session management (V3), and cloud access control (V4) are not applicable to V1 local single-user mode and are deferred to future cloud milestones.

---

## 4. Zero Secrets Policy

- Never commit passwords, API keys, credentials, or private certificates into source control.
- All environment configurations must be supplied via local environment variables or uncommitted `.env` files.
- Sample environment variables are documented safely in `.env.example`.
