#!/bin/bash
# Writ Backend Setup Script
# Generates security keys and sets up environment

set -e

echo "======================================"
echo "Writ Backend Setup"
echo "======================================"
echo ""

# Check if .env already exists
if [ -f ".env" ]; then
    echo "⚠️  .env file already exists!"
    read -p "Do you want to overwrite it? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Setup cancelled. Existing .env preserved."
        exit 0
    fi
    mv .env .env.backup.$(date +%Y%m%d_%H%M%S)
    echo "✓ Existing .env backed up"
fi

# Copy template
cp .env.example .env
echo "✓ Created .env from template"

# Generate API Secret Key
echo ""
echo "Generating API Secret Key..."
API_SECRET=$(openssl rand -hex 32)
sed -i.bak "s/API_SECRET_KEY=.*/API_SECRET_KEY=$API_SECRET/" .env
echo "✓ API_SECRET_KEY generated"

# Generate HMAC Secret Key
echo "Generating HMAC Secret Key..."
HMAC_SECRET=$(openssl rand -hex 32)
sed -i.bak "s/HMAC_SECRET_KEY=.*/HMAC_SECRET_KEY=$HMAC_SECRET/" .env
echo "✓ HMAC_SECRET_KEY generated"

# Generate Secret Encryption Key
echo "Generating Secret Encryption Key (Fernet)..."
SECRET_ENC_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
sed -i.bak "s/SECRET_ENCRYPTION_KEY=.*/SECRET_ENCRYPTION_KEY=$SECRET_ENC_KEY/" .env
echo "✓ SECRET_ENCRYPTION_KEY generated"

# Generate JWT signing secret
echo "Generating JWT signing secret..."
JWT_SECRET=$(openssl rand -hex 32)
sed -i.bak "s/JWT_SECRET_KEY=.*/JWT_SECRET_KEY=$JWT_SECRET/" .env
echo "✓ JWT_SECRET_KEY generated"

# Generate fleet-connect (recorder auth) secret
echo "Generating fleet-connect secret..."
RECORDER_SECRET=$(openssl rand -hex 32)
sed -i.bak "s/RECORDER_AUTH_SECRET=.*/RECORDER_AUTH_SECRET=$RECORDER_SECRET/" .env
echo "✓ RECORDER_AUTH_SECRET generated"

# Clean up backup files
rm -f .env.bak

echo ""
echo "======================================"
echo "✅ Setup Complete!"
echo "======================================"
echo ""
echo "Generated keys:"
echo "  - API_SECRET_KEY"
echo "  - HMAC_SECRET_KEY"
echo "  - SECRET_ENCRYPTION_KEY"
echo "  - JWT_SECRET_KEY"
echo "  - RECORDER_AUTH_SECRET"
echo ""
echo "⚠️  IMPORTANT:"
echo "  1. Back up your SECRET_ENCRYPTION_KEY securely!"
echo "     (Losing it makes every stored secret unreadable.)"
echo "  2. No database or Redis server to configure: the coordinator runs on a"
echo "     single SQLite file (WRIT_DB_PATH) with in-process fakeredis."
echo "  3. Set CORS_ORIGINS / ALLOWED_HOSTS to your real host for production."
echo "     ENVIRONMENT defaults to production; set ENVIRONMENT=development only"
echo "     for a throwaway local trial."
echo ""
echo "Next steps:"
echo "  1. Review .env (admin email/password, public URL)"
echo "  2. Run: alembic upgrade head"
echo "  3. Run: python3 serve.py   (single worker — do not add uvicorn workers)"
echo ""
echo "📖 See ../README.md and ../docs/ for full instructions"
echo ""
