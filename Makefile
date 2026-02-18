.PHONY: install uninstall

install:
	@echo "📦 Installing dt-setup command..."
	@chmod +x dt-setup configure.py
	@sudo cp dt-setup /usr/local/bin/dt-setup
	@echo "✅ Installation complete!"
	@echo ""
	@echo "You can now run from anywhere:"
	@echo "  dt-setup              # Full wizard"
	@echo "  dt-setup email        # Configure email"
	@echo "  dt-setup telegram     # Configure Telegram"
	@echo "  dt-setup core         # Configure API keys"
	@echo ""

uninstall:
	@echo "🗑️  Removing dt-setup command..."
	@sudo rm -f /usr/local/bin/dt-setup
	@echo "✅ Uninstalled successfully"
