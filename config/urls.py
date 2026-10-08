from django.contrib import admin
from django.urls import include, path
from django.views.generic import TemplateView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("routes.urls")),
    path("", TemplateView.as_view(template_name="index.html"), name="demo"),
]
