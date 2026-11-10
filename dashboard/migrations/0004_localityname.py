from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0003_remove_irrigation")]

    operations = [
        migrations.CreateModel(
            name="LocalityName",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("coordinates", models.CharField(max_length=48, unique=True)),
                ("local_name", models.CharField(max_length=255)),
                ("english_name", models.CharField(max_length=255)),
                ("names", models.JSONField(default=dict)),
            ],
        ),
    ]
