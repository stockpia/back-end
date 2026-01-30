from rest_framework import permissions
from rest_framework.views import APIView

# Create your views here.
class TestView(APIView):
    permission_classes = [permissions.AllowAny]
