from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator
from django.utils import timezone
import math
import qrcode
from io import BytesIO
from django.core.files import File
from PIL import Image, ImageDraw
import uuid

class User(AbstractUser):
    ROLE_CHOICES = [
        ('admin', 'Admin'),
        ('gso_staff', 'GSO Staff'),
        ('department_user', 'Department User'),
    ]
    
    APPROVAL_STATUS_CHOICES = [
        ('pending', 'Pending Approval'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='department_user')
    department = models.CharField(max_length=100, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    approval_status = models.CharField(max_length=20, choices=APPROVAL_STATUS_CHOICES, default='approved')
    profile_picture = models.ImageField(upload_to='profile_pictures/', blank=True, null=True)
    
    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"
    
    @property
    def is_approved(self):
        return self.approval_status == 'approved'
    
    @property
    def is_pending_approval(self):
        return self.approval_status == 'pending'

class SupplyCategory(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_material = models.BooleanField(default=False, help_text='Mark category as materials (e.g., tables, chairs) so they can be borrowed as equipment')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name_plural = "Supply Categories"
    
    def __str__(self):
        return self.name

class Supply(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField()
    category = models.ForeignKey(SupplyCategory, on_delete=models.CASCADE, related_name='supplies')
    quantity = models.PositiveIntegerField(default=0)
    min_stock_level = models.PositiveIntegerField(default=5)
    unit = models.CharField(max_length=50, default='pieces')
    cost_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    qr_code = models.ImageField(upload_to='qr_codes/', blank=True, null=True)
    image = models.ImageField(upload_to='supply_images/', blank=True, null=True, help_text="Product image or photo")
    location = models.CharField(max_length=100, default='Main Storage')
    is_consumable = models.BooleanField(default=False, help_text="Check if this item is consumable (e.g., paper, pens). Unchecked means non-consumable (e.g., equipment)")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name_plural = "Supplies"
    
    def __str__(self):
        return f"{self.name} ({self.quantity} {self.unit})"
    
    @property
    def is_low_stock(self):
        return self.quantity <= self.min_stock_level
    
    @property
    def stock_status(self):
        if self.quantity == 0:
            return 'out_of_stock'
        elif self.is_low_stock:
            return 'low_stock'
        else:
            return 'in_stock'
    
    def generate_qr_code(self):
        if not self.qr_code:
            qr_data = f"SUPPLY-{self.id}-{self.name}"
            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(qr_data)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            
            # Convert PIL image to RGB if it's not already
            img = img.convert('RGB') if hasattr(img, 'convert') else img
            
            # Get the size of the QR code image
            img_width, img_height = img.size
            
            # Create a canvas with enough space for the QR code and text
            canvas_width = max(300, img_width + 100)
            canvas_height = img_height + 100
            canvas = Image.new('RGB', (canvas_width, canvas_height), 'white')
            draw = ImageDraw.Draw(canvas)
            
            # Center the QR code on the canvas
            x_offset = (canvas_width - img_width) // 2
            y_offset = 20
            canvas.paste(img, (x_offset, y_offset))
            
            # Add supply name and ID
            text_y = y_offset + img_height + 10
            draw.text((20, text_y), f"{self.name[:30]}...", fill='black')
            draw.text((20, text_y + 20), f"ID: {self.id}", fill='black')
            
            buffer = BytesIO()
            canvas.save(buffer, 'PNG')
            buffer.seek(0)
            
            filename = f'supply_{self.id}_qr.png'
            self.qr_code.save(filename, File(buffer), save=False)
            self.save()

class SupplyRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('released', 'Released'),
        ('rejected', 'Rejected'),
    ]
    
    request_id = models.CharField(max_length=50, unique=True, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='supply_requests')
    supply = models.ForeignKey(Supply, on_delete=models.CASCADE, related_name='requests')
    quantity_requested = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    purpose = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_requests')
    approved_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='released_requests')
    released_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.TextField(blank=True, null=True)
    borrowing_qr_code = models.ImageField(upload_to='borrowing_qr_codes/', blank=True, null=True)
    requested_location = models.CharField(max_length=200, blank=True, null=True, help_text='Location where the requester intends to use the equipment')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Request {self.request_id} - {self.supply.name}"
    
    def save(self, *args, **kwargs):
        if not self.request_id:
            self.request_id = f"REQ-{timezone.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:8].upper()}"
        super().save(*args, **kwargs)
        
        # Generate QR code for borrowing requests
        if self.purpose.startswith('[BORROWING]') and not self.borrowing_qr_code:
            self.generate_borrowing_qr_code()
    
    def generate_borrowing_qr_code(self):
        """
        Generate a QR code for borrowing requests
        """
        if not self.purpose.startswith('[BORROWING]'):
            return
            
        # Create QR code data
        qr_data = f"BORROW-{self.id}-{self.user.id}-{self.supply.id}"
        
        # Generate QR code
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(qr_data)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Convert PIL image to RGB if it's not already
        img = img.convert('RGB') if hasattr(img, 'convert') else img
        
        # Get the size of the QR code image
        img_width, img_height = img.size
        
        # Create a canvas with enough space for the QR code and text
        canvas_width = max(300, img_width + 100)
        canvas_height = img_height + 100
        canvas = Image.new('RGB', (canvas_width, canvas_height), 'white')
        draw = ImageDraw.Draw(canvas)
        
        # Center the QR code on the canvas
        x_offset = (canvas_width - img_width) // 2
        y_offset = 20
        canvas.paste(img, (x_offset, y_offset))
        
        # Add request details
        text_y = y_offset + img_height + 10
        draw.text((20, text_y), f"{self.supply.name[:30]}...", fill='black')
        draw.text((20, text_y + 20), f"Request ID: {self.request_id}", fill='black')
        draw.text((20, text_y + 40), f"Requested by: {self.user.username}", fill='black')
        draw.text((20, text_y + 60), f"Quantity: {self.quantity_requested}", fill='black')
        
        buffer = BytesIO()
        canvas.save(buffer, 'PNG')
        buffer.seek(0)
        
        filename = f'borrowing_{self.id}_qr.png'
        self.borrowing_qr_code.save(filename, File(buffer), save=False)
        self.save()

class QRScanLog(models.Model):
    ACTION_CHOICES = [
        ('scan', 'Scan'),
        ('issue', 'Issue'),
        ('return', 'Return'),
    ]
    
    supply = models.ForeignKey(Supply, on_delete=models.CASCADE, related_name='scan_logs')
    scanned_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='qr_scans')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    location = models.CharField(max_length=100, default='Unknown')
    timestamp = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, null=True)
    
    class Meta:
        ordering = ['-timestamp']
    
    def __str__(self):
        return f"{self.action.upper()} - {self.supply.name} by {self.scanned_by.username}"

class InventoryTransaction(models.Model):
    TRANSACTION_TYPES = [
        ('in', 'Stock In'),
        ('out', 'Stock Out'),
        ('adjustment', 'Adjustment'),
    ]
    
    supply = models.ForeignKey(Supply, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    quantity = models.IntegerField()
    previous_quantity = models.PositiveIntegerField()
    new_quantity = models.PositiveIntegerField()
    reason = models.TextField()
    performed_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='inventory_transactions')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.transaction_type.upper()} - {self.supply.name} ({self.quantity})"

class BorrowedItem(models.Model):
    """
    Model to track non-consumable items that are borrowed and returned
    """
    supply = models.ForeignKey(Supply, on_delete=models.CASCADE, related_name='borrowed_items')
    borrower = models.ForeignKey(User, on_delete=models.CASCADE, related_name='borrowed_items')
    borrowed_date = models.DateField(help_text="Date when the item was borrowed")
    borrowed_at = models.DateTimeField(auto_now_add=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    borrowed_quantity = models.PositiveIntegerField(default=1)
    location_when_borrowed = models.CharField(max_length=100, default='Unknown')
    location_when_returned = models.CharField(max_length=100, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    return_deadline = models.DateField(null=True, blank=True, help_text="Date when the item must be returned (3 days from borrowed date)")
    borrow_duration_days = models.PositiveIntegerField(default=3, help_text="Number of days the item can be borrowed")
    
    class Meta:
        ordering = ['-borrowed_at']
    
    def __str__(self):
        return f"{self.supply.name} borrowed by {self.borrower.username}"
    
    @property
    def is_returned(self):
        return self.returned_at is not None
    
    def save(self, *args, **kwargs):
        """Auto-calculate return deadline if not set"""
        if not self.return_deadline and self.borrowed_date:
            self.return_deadline = self.borrowed_date + timezone.timedelta(days=self.borrow_duration_days)
        super().save(*args, **kwargs)
    
    @property
    def is_overdue(self):
        """Check if the item is overdue"""
        if self.is_returned or not self.return_deadline:
            return False
        return timezone.now().date() > self.return_deadline
    
    @property
    def days_until_due(self):
        """Calculate days until the item is due"""
        if self.is_returned or not self.return_deadline:
            return None
        # Return integer days remaining (can be 0 if due within 24h, negative if overdue)
        delta = self.return_deadline - timezone.now().date()
        return delta.days

    @property
    def due_in_days(self):
        """Return remaining time until due in fractional days (float). Negative when overdue."""
        if self.is_returned or not self.return_deadline:
            return None
        delta = self.return_deadline - timezone.now().date()
        return float(delta.days)

    @property
    def due_status(self):
        """Human-friendly status for the due state.

        Values:
          - 'no_deadline' : no deadline set
          - 'returned'    : already returned
          - 'overdue'     : past the deadline
          - 'due_today'   : due today (same date)
          - 'due_soon'    : due within threshold (3 days)
          - 'on_time'     : due later than threshold
        """
        if self.is_returned:
            return 'returned'
        if not self.return_deadline:
            return 'no_deadline'

        today = timezone.now().date()
        if today > self.return_deadline:
            return 'overdue'

        # Due today if same calendar date
        if self.return_deadline == today:
            return 'due_today'

        # Consider due soon when within this many days
        DUE_SOON_THRESHOLD = 3
        if self.due_in_days is not None and self.due_in_days <= DUE_SOON_THRESHOLD:
            return 'due_soon'

        return 'on_time'
    
    @property
    def duration(self):
        """
        Calculate the duration the item was borrowed.

        Returns duration in seconds. If the item is still borrowed, returns
        the time elapsed since borrowing (i.e., now - borrowed_at).
        """
        if not self.borrowed_at:
            return None

        end = self.returned_at if self.is_returned and self.returned_at else timezone.now()
        return (end - self.borrowed_at).total_seconds()
    
    @property
    def duration_display(self):
        """
        Return a human-readable duration string
        """
        duration_seconds = self.duration
        if duration_seconds is None:
            return "Unknown"

        days = int(duration_seconds // (24 * 3600))
        hours = int((duration_seconds % (24 * 3600)) // 3600)
        minutes = int((duration_seconds % 3600) // 60)

        parts = []
        if days > 0:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours > 0:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0 and days == 0:
            # show minutes only when duration is less than a day for brevity
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

        if not parts:
            return "less than a minute"

        # Prefix with context depending on return state
        label = "Borrowed for" if not self.is_returned else "Borrowed for"
        return f"{', '.join(parts)}"


class Notification(models.Model):
    LEVEL_CHOICES = [
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
    ]

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=200)
    message = models.TextField()
    url = models.CharField(max_length=400, blank=True, null=True)
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='info')
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Notification to {self.recipient.username}: {self.title}"

class RequestorBorrowerAnalytics(models.Model):
    """
    Tracks analytics for requestors and borrowers
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='analytics')
    total_requests = models.PositiveIntegerField(default=0)
    total_borrowings = models.PositiveIntegerField(default=0)
    approved_requests = models.PositiveIntegerField(default=0)
    rejected_requests = models.PositiveIntegerField(default=0)
    returned_items = models.PositiveIntegerField(default=0)
    overdue_items = models.PositiveIntegerField(default=0)
    last_request_date = models.DateTimeField(null=True, blank=True)
    last_borrow_date = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name_plural = "Requestor/Borrower Analytics"
    
    def __str__(self):
        return f"Analytics for {self.user.username}"

class UserActivityLog(models.Model):
    """
    Tracks daily, weekly, monthly, yearly activity for users
    """
    ACTIVITY_TYPE_CHOICES = [
        ('request', 'Supply Request'),
        ('borrow', 'Borrow Item'),
        ('return', 'Return Item'),
        ('approval', 'Request Approval'),
        ('rejection', 'Request Rejection'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='activity_logs')
    activity_type = models.CharField(max_length=20, choices=ACTIVITY_TYPE_CHOICES)
    supply = models.ForeignKey(Supply, on_delete=models.SET_NULL, null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    description = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['user', 'timestamp']),
            models.Index(fields=['activity_type', 'timestamp']),
        ]
    
    def __str__(self):
        return f"{self.user.username} - {self.activity_type} on {self.timestamp.date()}"

class MostRequestedItem(models.Model):
    """
    Tracks most requested/borrowed items and their frequency
    """
    supply = models.OneToOneField(Supply, on_delete=models.CASCADE, related_name='request_stats')
    request_count = models.PositiveIntegerField(default=0)
    borrow_count = models.PositiveIntegerField(default=0)
    last_requested = models.DateTimeField(null=True, blank=True)
    last_borrowed = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-request_count', '-borrow_count']
    
    def __str__(self):
        return f"{self.supply.name} - Requests: {self.request_count}, Borrows: {self.borrow_count}"
