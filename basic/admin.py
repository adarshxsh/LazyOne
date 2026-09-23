from django.contrib import admin
from .models import JuryPanel, JuryMember, Dispute

admin.site.register(JuryPanel)
admin.site.register(JuryMember)
admin.site.register(Dispute)

