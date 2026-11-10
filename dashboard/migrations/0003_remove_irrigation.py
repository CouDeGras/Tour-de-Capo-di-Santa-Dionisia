from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0002_irrigationdecision_station")]
    operations = [migrations.DeleteModel(name="IrrigationDecision")]
