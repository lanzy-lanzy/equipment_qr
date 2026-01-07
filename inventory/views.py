from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.urls import reverse
from django.db.models import Q, Count, Sum, F, Exists, OuterRef
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django_htmx.http import HttpResponseClientRefresh
import json
import uuid
import csv
from io import StringIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from django.conf import settings

# Optional Gemini import for AI suggestions
try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except (ImportError, TypeError) as e:
    GENAI_AVAILABLE = False
    print(f"[WARNING] Gemini API not available: {type(e).__name__}: {e}")

from .models import (
    User, Supply, SupplyCategory, SupplyRequest, 
    QRScanLog, InventoryTransaction, BorrowedItem
)
from .models import Notification
from .forms import (
    CustomUserCreationForm, SupplyForm, SupplyRequestForm, 
    SupplyCategoryForm, QRScanForm, BorrowedItemForm, BorrowRequestForm
)
from .forms import UserProfileForm
from .utils import check_low_stock_alerts, has_overdue_items, get_user_overdue_items, ensure_low_stock_notifications
from django.views.decorators.http import require_POST


def register_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    
    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            if getattr(user, 'is_pending_approval', False):
                messages.info(request, f'Account created successfully for {user.username}! Your account is pending admin approval.')
            else:
                messages.success(request, f'Account created successfully for {user.username}! You can now log in.')
            return redirect('login')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = CustomUserCreationForm()
    
    return render(request, 'inventory/register.html', {'form': form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            # Check if user is approved
            if not getattr(user, 'is_approved', True):
                messages.error(request, 'Your account is pending admin approval.')
                return render(request, 'inventory/login.html')
            login(request, user)
            return redirect('dashboard')
        else:
            messages.error(request, 'Invalid username or password')
    
    return render(request, 'inventory/login.html')


@login_required
def profile_update(request):
    user = request.user
    if request.method == 'POST':
        form = UserProfileForm(request.POST, request.FILES, instance=user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Your profile has been updated.')
            return redirect('profile_update')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = UserProfileForm(instance=user)

    return render(request, 'inventory/profile.html', {'form': form})

@login_required
def dashboard(request):
    user = request.user
    
    # Proactively check for low stock items and notify staff
    if user.role in ['admin', 'gso_staff']:
        ensure_low_stock_notifications()

    context = {
        'user': user,
        'total_supplies': Supply.objects.count(),
        'low_stock_count': Supply.objects.filter(quantity__lte=F('min_stock_level')).count(),
    }
    
    # Role-specific request counts
    if user.role == 'admin':
        context['total_requests'] = SupplyRequest.objects.count()
        context['pending_requests_count'] = SupplyRequest.objects.filter(status='pending').count()
        context['recent_requests'] = SupplyRequest.objects.order_by('-created_at')[:10]
        context['low_stock_items'] = Supply.objects.filter(quantity__lte=F('min_stock_level'))[:10]
        context['recently_borrowed'] = BorrowedItem.objects.filter(returned_at__isnull=True).order_by('-borrowed_at')[:10]
    elif user.role == 'gso_staff':
        context['total_requests'] = SupplyRequest.objects.count()
        context['pending_requests_count'] = SupplyRequest.objects.filter(status='pending').count()
        context['gso_pending_requests'] = SupplyRequest.objects.filter(status='pending').order_by('-created_at')[:10]
        context['recent_approvals'] = SupplyRequest.objects.filter(approved_by=user).order_by('-approved_at')[:5]
        context['low_stock_items'] = Supply.objects.filter(quantity__lte=F('min_stock_level'))[:10]
        context['recently_borrowed'] = BorrowedItem.objects.filter(returned_at__isnull=True).order_by('-borrowed_at')[:10]
    else:  # department_user
        # For department users, only show their own requests
        user_requests = SupplyRequest.objects.filter(user=user)
        context['total_requests'] = user_requests.count()
        context['pending_requests_count'] = user_requests.filter(status='pending').count()
        context['my_requests'] = user_requests.order_by('-created_at')[:10]
        context['my_borrowed_items'] = BorrowedItem.objects.filter(borrower=user, returned_at__isnull=True).order_by('-borrowed_at')[:10]
        context['has_overdue_items'] = has_overdue_items(user)
        context['overdue_items'] = get_user_overdue_items(user)
    
    return render(request, 'inventory/dashboard.html', context)


@login_required
@require_POST
def mark_all_notifications_read(request):
    """Mark all unread notifications for the current user as read."""
    try:
        Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        return JsonResponse({'success': True, 'message': 'Marked all notifications as read'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
def supply_list(request):
    # Ensure admins/GSO staff have low-stock notifications for current low supplies
    try:
        ensure_low_stock_notifications()
    except Exception:
        # Fail silently to avoid breaking the view if notification creation fails
        pass

    supplies = Supply.objects.all()
    categories = SupplyCategory.objects.all()
    
    # Get low stock count
    low_stock_count = Supply.objects.filter(quantity__lte=F('min_stock_level')).count()
    
    # Search functionality - enhanced search
    search = request.GET.get('search', '').strip()
    # Handle "undefined" values from HTMX
    if search == 'undefined':
        search = ''
    if search:
        # Split search terms to allow for multi-word searches
        search_terms = search.split()
        search_query = Q()
        
        # Build a query that matches any of the search terms in any of the fields
        for term in search_terms:
            term_query = (
                Q(name__icontains=term) | 
                Q(description__icontains=term) |
                Q(category__name__icontains=term) |
                Q(location__icontains=term) |
                Q(unit__icontains=term)
            )
            search_query &= term_query
        
        supplies = supplies.filter(search_query)
    
    # Filter by category
    category_filter = request.GET.get('category', '')
    # Handle "undefined" values from HTMX
    if category_filter == 'undefined':
        category_filter = ''
    if category_filter:
        try:
            # Ensure category_filter is a valid integer before filtering
            category_id = int(category_filter)
            supplies = supplies.filter(category_id=category_id)
        except (ValueError, TypeError):
            # Handle invalid category IDs
            category_filter = ''
    
    # Filter by stock status
    stock_filter = request.GET.get('stock', '')
    # Handle "undefined" values from HTMX
    if stock_filter == 'undefined':
        stock_filter = ''
    if stock_filter:
        if stock_filter == 'low':
            supplies = supplies.filter(quantity__lte=F('min_stock_level'))
        elif stock_filter == 'out':
            supplies = supplies.filter(quantity=0)
        elif stock_filter == 'available':
            supplies = supplies.filter(quantity__gt=F('min_stock_level'))
    
    # Add sorting options
    sort_by = request.GET.get('sort', 'name')
    sort_order = request.GET.get('order', 'asc')
    
    # Handle "undefined" values from HTMX
    if sort_by == 'undefined':
        sort_by = 'name'
    if sort_order == 'undefined':
        sort_order = 'asc'
    
    if sort_order == 'desc':
        sort_prefix = '-'
    else:
        sort_prefix = ''
    
    if sort_by == 'name':
        supplies = supplies.order_by(f'{sort_prefix}name')
    elif sort_by == 'category':
        supplies = supplies.order_by(f'{sort_prefix}category__name')
    elif sort_by == 'quantity':
        supplies = supplies.order_by(f'{sort_prefix}quantity')
    elif sort_by == 'location':
        supplies = supplies.order_by(f'{sort_prefix}location')
    elif sort_by == 'created_at':
        supplies = supplies.order_by(f'{sort_prefix}created_at')
    
    context = {
        'supplies': supplies,
        'categories': categories,
        'search': search,
        'category_filter': category_filter,
        'stock_filter': stock_filter,
        'low_stock_count': low_stock_count,
        'sort_by': sort_by,
        'sort_order': sort_order,
    }
    
    if request.htmx:
        return render(request, 'inventory/partials/supply_list.html', context)
    
    return render(request, 'inventory/supply_list.html', context)

@login_required
def supply_detail(request, pk):
    from django.db.models import Sum
    
    supply = get_object_or_404(Supply, pk=pk)
    recent_transactions = supply.transactions.order_by('-created_at')[:10]
    recent_scans = supply.scan_logs.order_by('-timestamp')[:10]
    
    # The available quantity is simply the current supply quantity (stock on hand)
    available_quantity = supply.quantity
    
    # Calculate total borrowed items for non-consumable supplies
    borrowed_total = 0
    if not supply.is_consumable:
        borrowed_total = BorrowedItem.objects.filter(
            supply=supply,
            returned_at__isnull=True
        ).aggregate(total=Sum('borrowed_quantity'))['total'] or 0
    
    context = {
        'supply': supply,
        'recent_transactions': recent_transactions,
        'recent_scans': recent_scans,
        'available_quantity': available_quantity,
        'borrowed_total': borrowed_total,
    }
    
    return render(request, 'inventory/supply_detail.html', context)

@login_required
def supply_create(request):
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to create supplies.')
        return redirect('supply_list')
    
    if request.method == 'POST':
        form = SupplyForm(request.POST, request.FILES)
        if form.is_valid():
            supply = form.save()
            supply.generate_qr_code()
            messages.success(request, f'Supply "{supply.name}" created successfully.')
            return redirect('supply_detail', pk=supply.pk)
    else:
        form = SupplyForm()
    
    return render(request, 'inventory/supply_form.html', {'form': form, 'action': 'Create'})

@login_required
def supply_import(request):
    """Display CSV import page"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to import supplies.')
        return redirect('supply_list')
    
    context = {
        'categories': SupplyCategory.objects.all()
    }
    return render(request, 'inventory/supply_import.html', context)

@login_required
@require_POST
def supply_import_process(request):
    """Process CSV file upload and import supplies"""
    if request.user.role not in ['admin', 'gso_staff']:
        return JsonResponse({'success': False, 'error': 'You do not have permission to import supplies.'}, status=403)
    
    if 'csv_file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file provided'})
    
    csv_file = request.FILES['csv_file']
    
    if not csv_file.name.endswith('.csv'):
        return JsonResponse({'success': False, 'error': 'Please upload a CSV file'})
    
    try:
        # Read CSV file
        decoded_file = csv_file.read().decode('utf-8')
        csv_reader = csv.DictReader(StringIO(decoded_file))
        
        if not csv_reader.fieldnames:
            return JsonResponse({'success': False, 'error': 'CSV file is empty'})
        
        # Validate required columns
        required_columns = {'name', 'category', 'quantity', 'unit', 'description'}
        csv_columns = set(csv_reader.fieldnames)
        
        if not required_columns.issubset(csv_columns):
            missing = required_columns - csv_columns
            return JsonResponse({
                'success': False, 
                'error': f'Missing required columns: {", ".join(missing)}'
            })
        
        # Process rows
        created_count = 0
        updated_count = 0
        errors = []
        
        for row_num, row in enumerate(csv_reader, start=2):  # Start at 2 because row 1 is header
            try:
                # Get category
                category_name = row.get('category', '').strip()
                if not category_name:
                    errors.append(f"Row {row_num}: Category is required")
                    continue
                
                category, _ = SupplyCategory.objects.get_or_create(name=category_name)
                
                # Get or create supply
                supply_name = row.get('name', '').strip()
                if not supply_name:
                    errors.append(f"Row {row_num}: Supply name is required")
                    continue
                
                quantity = int(row.get('quantity', 0))
                unit = row.get('unit', 'pieces').strip()
                description = row.get('description', '').strip()
                min_stock = int(row.get('min_stock_level', 5))
                cost_per_unit = float(row.get('cost_per_unit', 0.00))
                location = row.get('location', 'Main Storage').strip()
                is_consumable = row.get('is_consumable', 'false').lower() in ('true', 'yes', '1')
                
                supply, created = Supply.objects.update_or_create(
                    name=supply_name,
                    defaults={
                        'category': category,
                        'quantity': quantity,
                        'unit': unit,
                        'description': description,
                        'min_stock_level': min_stock,
                        'cost_per_unit': cost_per_unit,
                        'location': location,
                        'is_consumable': is_consumable,
                    }
                )
                
                # Generate QR code if new
                if created:
                    supply.generate_qr_code()
                    created_count += 1
                else:
                    updated_count += 1
                    
            except ValueError as e:
                errors.append(f"Row {row_num}: Invalid data - {str(e)}")
            except Exception as e:
                errors.append(f"Row {row_num}: {str(e)}")
        
        return JsonResponse({
            'success': True,
            'created': created_count,
            'updated': updated_count,
            'errors': errors
        })
        
    except csv.Error as e:
        return JsonResponse({'success': False, 'error': f'CSV parsing error: {str(e)}'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'Error processing file: {str(e)}'})

@login_required
def supply_edit(request, pk):
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to edit supplies.')
        return redirect('supply_list')
    
    supply = get_object_or_404(Supply, pk=pk)
    
    if request.method == 'POST':
        # Store previous quantity before saving
        previous_quantity = supply.quantity
        form = SupplyForm(request.POST, request.FILES, instance=supply)
        if form.is_valid():
            supply = form.save()
            messages.success(request, f'Supply "{supply.name}" updated successfully.')
            
            # Check for low stock alert if quantity changed
            if previous_quantity != supply.quantity:
                alert_message = check_low_stock_alerts(supply, previous_quantity, supply.quantity)
                if alert_message:
                    messages.warning(request, alert_message)
            
            return redirect('supply_detail', pk=supply.pk)
    else:
        form = SupplyForm(instance=supply)
    
    return render(request, 'inventory/supply_form.html', {'form': form, 'supply': supply, 'action': 'Edit'})

@login_required
def request_list(request):
    user = request.user
    
    if user.role == 'admin':
        requests_qs = SupplyRequest.objects.all()
    elif user.role == 'gso_staff':
        requests_qs = SupplyRequest.objects.all()
    else:  # department_user
        requests_qs = SupplyRequest.objects.filter(user=user)
    
    # Filter by status
    status_filter = request.GET.get('status', '')
    if status_filter:
        requests_qs = requests_qs.filter(status=status_filter)
    
    # Search functionality
    search = request.GET.get('search', '')
    if search:
        requests_qs = requests_qs.filter(
            Q(request_id__icontains=search) |
            Q(supply__name__icontains=search) |
            Q(user__username__icontains=search)
        )
    
    # Grouping requests (by date and user for simple grouping if no group_id)
    # In a real app, you'd have a Batch/RequestGroup model.
    # Here we'll group by a combination of created_at date and user.
    from collections import defaultdict
    grouped_requests = defaultdict(list)
    
    for req in requests_qs.order_by('-created_at'):
        # If purpose has a special pattern like [BATCH:xyz], use that
        # Otherwise group by (user, date)
        group_key = (req.user_id, req.created_at.strftime('%Y-%m-%d %H:%M'))
        grouped_requests[group_key].append(req)
    
    processed_groups = []
    for key, items in grouped_requests.items():
        first_item = items[0]
        # Determine status of the group
        statuses = set(item.status for item in items)
        if len(statuses) == 1:
            group_status = statuses.pop()
        else:
            group_status = 'mixed'
            
        is_borrowing = any(item.purpose.startswith('[BORROWING]') for item in items)
        
        processed_groups.append({
            'id': f"{first_item.user_id}-{first_item.created_at.strftime('%Y%m%d%H%M')}",
            'group_id': None, # Could be implemented if needed
            'items': items,
            'user': first_item.user,
            'status': group_status,
            'created_at': first_item.created_at,
            'is_borrowing': is_borrowing
        })
    
    # Sort groups by date
    processed_groups.sort(key=lambda x: x['created_at'], reverse=True)
    
    context = {
        'requests': processed_groups,
        'status_filter': status_filter,
        'search': search,
    }
    
    if request.htmx:
        return render(request, 'inventory/partials/request_list.html', context)
    
    return render(request, 'inventory/request_list.html', context)

@login_required
def request_create(request):
    if request.user.role not in ['department_user', 'admin']:
        messages.error(request, 'Only department users can create requests.')
        return redirect('request_list')
    
    if request.method == 'POST':
        form = SupplyRequestForm(request.POST, user=request.user)
        if form.is_valid():
            supply_request = form.save(commit=False)
            supply_request.user = request.user
            supply_request.save()
            messages.success(request, f'Request {supply_request.request_id} created successfully.')
            return redirect('request_detail', pk=supply_request.pk)
    else:
        form = SupplyRequestForm(user=request.user)
    
    # Prepare supplies data for the template - only consumable items with stock
    supplies_data = [
        {
            'id': s.pk,
            'name': s.name,
            'stock': s.quantity,
            'unit': s.unit or 'pieces',
            'is_consumable': True
        }
        for s in Supply.objects.filter(quantity__gt=0, is_consumable=True).order_by('name')
    ]
    
    context = {
        'form': form,
        'action': 'Create',
        'supplies_json': json.dumps(supplies_data)
    }
    
    return render(request, 'inventory/request_form.html', context)

@login_required
def request_detail(request, pk):
    supply_request = get_object_or_404(SupplyRequest, pk=pk)
    
    # Check permissions
    user = request.user
    if user.role == 'department_user' and supply_request.user != user:
        messages.error(request, 'You can only view your own requests.')
        return redirect('request_list')
    
    context = {
        'supply_request': supply_request,
    }
    
    return render(request, 'inventory/request_detail.html', context)

@login_required
@require_http_methods(['POST'])
def request_approve(request, pk):
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'Unauthorized')
        return redirect('request_list')
    
    supply_request = get_object_or_404(SupplyRequest, pk=pk)
    
    if supply_request.status != 'pending':
        messages.error(request, 'Request is not pending')
        return redirect('request_detail', pk=pk)
    
    # Check if this is a borrowing request
    is_borrowing_request = supply_request.purpose.startswith('[BORROWING]')
    
    if is_borrowing_request:
        # For borrowing requests, redirect to a form where GSO staff can set dates
        return redirect('approve_borrow_request', pk=pk)
    else:
        # Regular supply request approval
        supply_request.status = 'approved'
        supply_request.approved_by = request.user
        supply_request.approved_at = timezone.now()
        supply_request.save()
        
        if request.htmx:
            messages.success(request, f'Request {supply_request.request_id} approved successfully.')
            return HttpResponseClientRefresh()
        
        messages.success(request, f'Request {supply_request.request_id} approved successfully.')
        # Redirect back to the referring page or request detail
        next_url = request.GET.get('next') or request.META.get('HTTP_REFERER')
        if next_url:
            return redirect(next_url)
        return redirect('request_detail', pk=pk)

@login_required
@require_http_methods(['POST'])
def request_reject(request, pk):
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'Unauthorized')
        return redirect('request_list')
    
    supply_request = get_object_or_404(SupplyRequest, pk=pk)
    
    if supply_request.status != 'pending':
        messages.error(request, 'Request is not pending')
        return redirect('request_detail', pk=pk)
    
    reason = request.POST.get('reason', '')
    
    supply_request.status = 'rejected'
    supply_request.rejected_reason = reason
    supply_request.approved_by = request.user
    supply_request.approved_at = timezone.now()
    supply_request.save()
    
    if request.htmx:
        messages.success(request, f'Request {supply_request.request_id} rejected.')
        return HttpResponseClientRefresh()
    
    messages.success(request, f'Request {supply_request.request_id} rejected.')
    # Redirect back to the referring page or request detail
    next_url = request.GET.get('next') or request.META.get('HTTP_REFERER')
    if next_url:
        return redirect(next_url)
    return redirect('request_detail', pk=pk)

@login_required
@require_http_methods(['POST'])
def request_release(request, pk):
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'Unauthorized')
        return redirect('request_list')
    
    supply_request = get_object_or_404(SupplyRequest, pk=pk)
    
    if supply_request.status != 'approved':
        messages.error(request, 'Request must be approved first')
        return redirect('request_detail', pk=pk)
    
    if supply_request.supply.quantity < supply_request.quantity_requested:
        messages.error(request, 'Insufficient stock')
        return redirect('request_detail', pk=pk)
    
    # Update supply quantity
    previous_quantity = supply_request.supply.quantity
    supply_request.supply.quantity -= supply_request.quantity_requested
    supply_request.supply.save()
    
    # Check for low stock alert
    new_quantity = supply_request.supply.quantity
    alert_message = check_low_stock_alerts(supply_request.supply, previous_quantity, new_quantity)
    if alert_message:
        messages.warning(request, alert_message)
    
    # Update request status
    supply_request.status = 'released'
    supply_request.released_by = request.user
    supply_request.released_at = timezone.now()
    supply_request.save()

    # Create BorrowedItem record to track the issued items
    try:
        BorrowedItem.objects.create(
            supply=supply_request.supply,
            borrower=supply_request.user,
            borrowed_quantity=supply_request.quantity_requested,
            borrowed_date=timezone.now().date(),
            location_when_borrowed=supply_request.supply.location or '',
            notes=f"Released for request {supply_request.request_id}"
        )
    except Exception:
        # If creating the borrowed item fails, log but continue with transaction
        pass
    
    # Log the transaction
    InventoryTransaction.objects.create(
        supply=supply_request.supply,
        transaction_type='out',
        quantity=-supply_request.quantity_requested,
        previous_quantity=previous_quantity,
        new_quantity=supply_request.supply.quantity,
        reason=f"Released for request {supply_request.request_id}",
        performed_by=request.user
    )
    
    if request.htmx:
        messages.success(request, f'Request {supply_request.request_id} released successfully.')
        return HttpResponseClientRefresh()
    
    messages.success(request, f'Request {supply_request.request_id} released successfully.')
    # Redirect back to the referring page or request detail
    next_url = request.GET.get('next') or request.META.get('HTTP_REFERER')
    if next_url:
        return redirect(next_url)
    return redirect('request_detail', pk=pk)

@login_required
def qr_scanner(request):
    return render(request, 'inventory/qr_scanner.html')

@login_required
def get_recent_scans(request):
    """API endpoint to fetch recent QR scans for the current user"""
    try:
        # Get recent scans for the current user, limited to 10
        recent_scans = QRScanLog.objects.filter(
            scanned_by=request.user
        ).select_related(
            'supply', 'scanned_by'
        ).order_by('-timestamp')[:10]
        
        # Format the data for JSON response
        scans_data = []
        for scan in recent_scans:
            scans_data.append({
                'supply': {
                    'name': scan.supply.name,
                    'id': scan.supply.id
                },
                'action': scan.action,
                'scanned_by': {
                    'username': scan.scanned_by.username
                },
                'location': scan.location,
                'timestamp': scan.timestamp.isoformat()
            })
        
        return JsonResponse({
            'success': True,
            'recent_scans': scans_data
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)

@login_required
@require_http_methods(['POST'])
def process_qr_scan(request):
    # Parse JSON data if sent as JSON, otherwise use form data
    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body)
            form = QRScanForm(data)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    else:
        form = QRScanForm(request.POST)
    
    if form.is_valid():
        qr_data = form.cleaned_data['qr_data']
        action = form.cleaned_data['action']
        location = form.cleaned_data.get('location', '')  # Location is optional for scan
        notes = form.cleaned_data['notes']
        
        # Get quantity for issue/return actions
        quantity = 1  # Default to 1
        if action in ['issue', 'return']:
            # Try to get quantity from form data
            if request.content_type == 'application/json':
                quantity = int(data.get('quantity', 1))
            else:
                quantity = int(request.POST.get('quantity', 1))
            
            # Validate quantity
            if quantity < 1:
                return JsonResponse({'error': 'Quantity must be at least 1'}, status=400)
        
        # Extract supply ID from QR data
        try:
            # Check if this is a borrowing QR code
            print(f"Processing QR data: {qr_data}")  # Debugging
            if qr_data.startswith('BORROW-'):
                # This is a borrowing request QR code
                print("BORROW- QR code detected")  # Debugging
                parts = qr_data.split('-')
                if len(parts) >= 4:
                    request_id = int(parts[1])
                    user_id = int(parts[2])
                    supply_id = int(parts[3])
                    
                    print(f"Parsed QR data - Request ID: {request_id}, User ID: {user_id}, Supply ID: {supply_id}")  # Debugging
                    
                    # Get the borrowing request
                    borrowing_request = get_object_or_404(SupplyRequest, id=request_id, user_id=user_id, supply_id=supply_id)
                    
                    print(f"Found borrowing request: {borrowing_request.id}")  # Debugging
                    
                    if action == 'scan':
                        # For scan action, just show borrowing request details
                        message = f"Borrowing Request: {borrowing_request.supply.name}\n"
                        message += f"Requested by: {borrowing_request.user.get_full_name() or borrowing_request.user.username}\n"
                        message += f"Quantity: {borrowing_request.quantity_requested}\n"
                        message += f"Status: {borrowing_request.get_status_display()}"
                        
                        print(f"Returning borrowing request details: {message}")  # Debugging
                        
                        # Determine if this supply currently has an active borrowed item
                        is_borrowed = BorrowedItem.objects.filter(
                            supply=borrowing_request.supply,
                            returned_at__isnull=True
                        ).exists()

                        return JsonResponse({
                            'success': True,
                            'supply': {
                                'id': borrowing_request.supply.id,
                                'name': borrowing_request.supply.name,
                                'quantity': borrowing_request.supply.quantity,
                                'location': borrowing_request.supply.location,
                                'action': action,
                                'timestamp': timezone.now().isoformat()
                            },
                            'borrowing_request': {
                                'id': borrowing_request.id,
                                'request_id': borrowing_request.request_id,
                                'user': borrowing_request.user.username,
                                'quantity_requested': borrowing_request.quantity_requested,
                                'status': borrowing_request.status,
                                'created_at': borrowing_request.created_at.strftime('%B %d, %Y')
                            },
                            'message': message,
                            'is_item_borrowed': is_borrowed
                        })
                    elif action == 'issue':
                        # Issue the borrowed item
                        supply = borrowing_request.supply
                        
                        # Check if there's enough quantity available
                        if supply.quantity >= borrowing_request.quantity_requested:
                            # Reduce supply quantity
                            previous_quantity = supply.quantity
                            supply.quantity -= borrowing_request.quantity_requested
                            supply.location = location
                            supply.save()
                            
                            # Log inventory transaction
                            InventoryTransaction.objects.create(
                                supply=supply,
                                transaction_type='out',
                                quantity=-borrowing_request.quantity_requested,
                                previous_quantity=previous_quantity,
                                new_quantity=supply.quantity,
                                reason=f"Issued for borrowing request {borrowing_request.request_id}",
                                performed_by=request.user
                            )
                            
                            # Check for low stock alert
                            new_quantity = supply.quantity
                            alert_message = check_low_stock_alerts(supply, previous_quantity, new_quantity)
                            if alert_message:
                                message += f' {alert_message}'
                            
                            # Mark the borrowing request as released
                            borrowing_request.status = 'released'
                            borrowing_request.released_by = request.user
                            borrowing_request.released_at = timezone.now()
                            borrowing_request.save()
                            
                            # Track borrowed item with the correct borrower
                            BorrowedItem.objects.create(
                                supply=supply,
                                borrower=borrowing_request.user,
                                borrowed_quantity=borrowing_request.quantity_requested,
                                borrowed_date=timezone.now().date(),
                                location_when_borrowed=location,
                                notes=notes
                            )
                            
                            message += f' Fulfilled borrowing request {borrowing_request.request_id}.'
                            
                            # Log the scan
                            scan_log = QRScanLog.objects.create(
                                supply=supply,
                                scanned_by=request.user,
                                action=action,
                                location=location,
                                notes=notes
                            )
                            
                            # Log inventory transaction
                            InventoryTransaction.objects.create(
                                supply=supply,
                                transaction_type='out',
                                quantity=-borrowing_request.quantity_requested,
                                previous_quantity=previous_quantity,
                                new_quantity=supply.quantity,
                                reason=f"Issued {borrowing_request.quantity_requested} items via QR scan for borrowing request {borrowing_request.request_id}",
                                performed_by=request.user
                            )
                            
                            # Get recent transaction history for this supply
                            recent_transactions = supply.transactions.order_by('-created_at')[:5]
                            transaction_history = [{
                                'transaction_type': t.transaction_type,
                                'quantity': t.quantity,
                                'created_at': t.created_at.isoformat()
                            } for t in recent_transactions]
                            
                            return JsonResponse({
                                'success': True,
                                'supply': {
                                    'id': supply.id,
                                    'name': supply.name,
                                    'quantity': supply.quantity,
                                    'location': supply.location,
                                    'action': action,
                                    'timestamp': scan_log.timestamp.isoformat()
                                },
                                'transaction_history': transaction_history,
                                'message': message
                            })
                        else:
                            return JsonResponse({'error': f'Insufficient stock. Only {supply.quantity} items available.'}, status=400)
                    elif action == 'return':
                        # Return the borrowed item
                        supply = borrowing_request.supply
                        
                        # Find the borrowed item record
                        try:
                            borrowed_item = BorrowedItem.objects.filter(
                                supply=supply,
                                borrower=borrowing_request.user,
                                returned_at__isnull=True
                            ).latest('borrowed_at')
                            
                            # Increase supply quantity
                            previous_quantity = supply.quantity
                            supply.quantity += borrowed_item.borrowed_quantity
                            supply.location = location
                            supply.save()
                            
                            # Mark borrowed item as returned
                            borrowed_item.returned_at = timezone.now()
                            borrowed_item.location_when_returned = location
                            borrowed_item.save()
                            
                            message = f'Supply {supply.name} returned successfully. Quantity increased by {borrowed_item.borrowed_quantity}.'
                            message += f' Borrowed for {borrowed_item.duration_display}.'
                            
                            # Log the scan
                            scan_log = QRScanLog.objects.create(
                                supply=supply,
                                scanned_by=request.user,
                                action=action,
                                location=location,
                                notes=notes
                            )
                            
                            # Log inventory transaction
                            InventoryTransaction.objects.create(
                                supply=supply,
                                transaction_type='in',
                                quantity=borrowed_item.borrowed_quantity,
                                previous_quantity=previous_quantity,
                                new_quantity=supply.quantity,
                                reason=f"Returned {borrowed_item.borrowed_quantity} items via QR scan for borrowing request {borrowing_request.request_id}",
                                performed_by=request.user
                            )
                            
                            # Get recent transaction history for this supply
                            recent_transactions = supply.transactions.order_by('-created_at')[:5]
                            transaction_history = [{
                                'transaction_type': t.transaction_type,
                                'quantity': t.quantity,
                                'created_at': t.created_at.isoformat()
                            } for t in recent_transactions]
                            
                            return JsonResponse({
                                'success': True,
                                'supply': {
                                    'id': supply.id,
                                    'name': supply.name,
                                    'quantity': supply.quantity,
                                    'location': supply.location,
                                    'action': action,
                                    'timestamp': scan_log.timestamp.isoformat()
                                },
                                'transaction_history': transaction_history,
                                'message': message
                            })
                        except BorrowedItem.DoesNotExist:
                            return JsonResponse({'error': 'No active borrowed item found for this request.'}, status=400)
                else:
                    return JsonResponse({'error': 'Invalid borrowing QR code format.'}, status=400)

            # If the frontend supplied a borrowing_request_id directly (JSON payload),
            # allow the scanner to operate on that request as well. This enables the
            # QR scanner to send an 'issue' action that marks a specific request as released.
            if request.content_type == 'application/json' and data.get('borrowing_request_id'):
                try:
                    br_id = int(data.get('borrowing_request_id'))
                except (TypeError, ValueError):
                    return JsonResponse({'error': 'Invalid borrowing_request_id'}, status=400)

                try:
                    borrowing_request = SupplyRequest.objects.get(pk=br_id)
                except SupplyRequest.DoesNotExist:
                    return JsonResponse({'error': 'Borrowing request not found'}, status=404)

                # If action is scan, return borrowing request summary (handled above for BORROW- too)
                if action == 'scan':
                    message = f"Borrowing Request: {borrowing_request.supply.name}\n"
                    message += f"Requested by: {borrowing_request.user.get_full_name() or borrowing_request.user.username}\n"
                    message += f"Quantity: {borrowing_request.quantity_requested}\n"
                    message += f"Status: {borrowing_request.get_status_display()}"

                    return JsonResponse({
                        'success': True,
                        'supply': {
                            'id': borrowing_request.supply.id,
                            'name': borrowing_request.supply.name,
                            'quantity': borrowing_request.supply.quantity,
                            'location': borrowing_request.supply.location,
                            'action': action,
                            'timestamp': timezone.now().isoformat()
                        },
                        'borrowing_request': {
                            'id': borrowing_request.id,
                            'request_id': borrowing_request.request_id,
                            'user': borrowing_request.user.username,
                            'quantity_requested': borrowing_request.quantity_requested,
                            'status': borrowing_request.status,
                            'created_at': borrowing_request.created_at.strftime('%B %d, %Y')
                        },
                        'message': message
                    })

                # If action is issue and mark_request_released is set, perform the release
                # NOTE: disallow marking requests released via the generic QR API to avoid
                # accidental releases from scanner/automated calls. Require using the
                # approve page or the explicit `request_release` endpoint.
                if action == 'issue' and data.get('mark_request_released'):
                    return JsonResponse({
                        'error': 'Releasing a borrowing request via QR API is disabled. Open the approve page to complete issuance.'
                    }, status=403)
                    
            
            # Handle regular supply QR codes
            if qr_data.startswith('SUPPLY-'):
                supply_id = int(qr_data.split('-')[1])
            else:
                supply_id = int(qr_data)
            
            supply = Supply.objects.get(pk=supply_id)
            
            # Store previous quantity for transaction logging
            previous_quantity = supply.quantity
            
            # Implement business logic based on action
            if action == 'issue':
                # Issue action - remove items from stock
                if supply.quantity >= quantity:
                    supply.quantity -= quantity
                    supply.location = location
                    supply.save()
                    message = f'Supply {supply.name} issued successfully. Quantity reduced by {quantity}.'
                    
                    # Check for low stock alert
                    new_quantity = supply.quantity
                    alert_message = check_low_stock_alerts(supply, previous_quantity, new_quantity)
                    if alert_message:
                        message += f' {alert_message}'
                    
                    # Check if this is for a borrowing request
                    # Find any approved borrowing requests for this supply
                    borrowing_request = SupplyRequest.objects.filter(
                        supply=supply,
                        status='approved',
                        purpose__startswith='[BORROWING]'
                    ).first()
                    
                    # Determine the borrower - if there's an approved borrowing request, use that user, otherwise use the current user
                    borrower = borrowing_request.user if borrowing_request else request.user
                    
                    if borrowing_request:
                        # This is a borrowing request fulfillment
                        message += f' Fulfilled borrowing request {borrowing_request.request_id}.'
                        # Mark the request as released (borrowed)
                        borrowing_request.status = 'released'
                        borrowing_request.released_by = request.user
                        borrowing_request.released_at = timezone.now()
                        borrowing_request.save()
                    
                    # Track borrowed item with the correct borrower
                    BorrowedItem.objects.create(
                        supply=supply,
                        borrower=borrower,  # Use the correct borrower (original requester for borrowing requests)
                        borrowed_quantity=quantity,
                        borrowed_date=timezone.now().date(),
                        location_when_borrowed=location,
                        notes=notes
                    )
                else:
                    return JsonResponse({'error': f'Insufficient stock. Only {supply.quantity} items available.'}, status=400)
            elif action == 'return':
                # Return action - add items back to stock
                supply.quantity += quantity
                supply.location = location
                supply.save()
                message = f'Supply {supply.name} returned successfully. Quantity increased by {quantity}.'
                
                # Update borrowed item record
                # Find the most recent borrowed item for this supply by this user that hasn't been returned yet
                try:
                    borrowed_item = BorrowedItem.objects.filter(
                        supply=supply,
                        borrower=request.user,
                        returned_at__isnull=True
                    ).latest('borrowed_at')
                    
                    borrowed_item.returned_at = timezone.now()
                    borrowed_item.location_when_returned = location
                    borrowed_item.save()
                    
                    # Add duration information to the message
                    message += f' Borrowed for {borrowed_item.duration_display}.'
                except BorrowedItem.DoesNotExist:
                    # If no borrowed item record exists, create one for tracking purposes
                    BorrowedItem.objects.create(
                        supply=supply,
                        borrower=request.user,
                        borrowed_quantity=quantity,
                        borrowed_date=timezone.now().date(),
                        location_when_borrowed='Unknown',  # We don't know where it was borrowed from
                        location_when_returned=location,
                        returned_at=timezone.now(),
                        notes=notes
                    )
            elif action == 'scan':
                # Scan action - just for tracking/location viewing
                # No changes to inventory, just log the scan
                message = f'Supply {supply.name} current location: {supply.location}. Total stock: {supply.quantity} units.'
            
            # Log the scan
            scan_log = QRScanLog.objects.create(
                supply=supply,
                scanned_by=request.user,
                action=action,
                location=location if action != 'scan' else supply.location,
                notes=notes
            )
            
            # Log inventory transaction for issue/return actions
            transaction_history = []
            if action in ['issue', 'return']:
                transaction_type = 'out' if action == 'issue' else 'in'
                quantity_change = -quantity if action == 'issue' else quantity
                reason = f"Issued {quantity} items via QR scan" if action == 'issue' else f"Returned {quantity} items via QR scan"
                
                InventoryTransaction.objects.create(
                    supply=supply,
                    transaction_type=transaction_type,
                    quantity=quantity_change,
                    previous_quantity=previous_quantity,
                    new_quantity=supply.quantity,
                    reason=reason,
                    performed_by=request.user
                )
                
                # Get recent transaction history for this supply
                recent_transactions = supply.transactions.order_by('-created_at')[:5]
                transaction_history = [{
                    'transaction_type': t.transaction_type,
                    'quantity': t.quantity,
                    'created_at': t.created_at.isoformat()
                } for t in recent_transactions]
            elif action == 'scan':
                # For scan action, get recent transaction history
                recent_transactions = supply.transactions.order_by('-created_at')[:5]
                transaction_history = [{
                    'transaction_type': t.transaction_type,
                    'quantity': t.quantity,
                    'created_at': t.created_at.isoformat()
                } for t in recent_transactions]
            
            # Check if item is currently borrowed (released but not returned)
            is_item_borrowed = False
            borrowed_item = BorrowedItem.objects.filter(
                supply=supply,
                returned_at__isnull=True  # Item is borrowed if returned_at is null
            ).order_by('-borrowed_at').first()
            
            if borrowed_item:
                is_item_borrowed = True
            
            return JsonResponse({
                'success': True,
                'supply': {
                    'id': supply.id,
                    'name': supply.name,
                    'quantity': supply.quantity,
                    'location': supply.location,
                    'action': action,
                    'timestamp': scan_log.timestamp.isoformat()
                },
                'transaction_history': transaction_history,
                'is_item_borrowed': is_item_borrowed,
                'message': message
            })
        
        except (ValueError, Supply.DoesNotExist):
            return JsonResponse({'error': 'Supply not found'}, status=404)
        except Exception as e:
            return JsonResponse({'error': f'Error processing scan: {str(e)}'}, status=500)
    
    return JsonResponse({'error': 'Invalid form data'}, status=400)

@login_required
def reports(request):
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to view reports.')
        return redirect('dashboard')
    
    # Get filter parameters
    report_type = request.GET.get('report_type', 'overview')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    search_query = request.GET.get('search', '')
    status_filter = request.GET.get('status', '')
    
    # Generate basic analytics
    total_supplies = Supply.objects.count()
    low_stock_items = Supply.objects.filter(quantity__lte=F('min_stock_level')).count()
    total_requests = SupplyRequest.objects.count()
    pending_requests = SupplyRequest.objects.filter(status='pending').count()
    released_requests = SupplyRequest.objects.filter(status='released').count()
    
    # Recent activity - apply filters
    recent_requests = SupplyRequest.objects.order_by('-created_at')[:10]
    recent_transactions = InventoryTransaction.objects.order_by('-created_at')[:10]
    
    # Filtered requests for report view
    filtered_requests = SupplyRequest.objects.all()
    filtered_transactions = InventoryTransaction.objects.all()
    filtered_supplies = Supply.objects.all()
    
    # Apply date filters
    if date_from:
        try:
            from datetime import datetime
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            filtered_requests = filtered_requests.filter(created_at__gte=date_from_obj)
            filtered_transactions = filtered_transactions.filter(created_at__gte=date_from_obj)
        except ValueError:
            pass
    
    if date_to:
        try:
            from datetime import datetime
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
            # Add 1 day to include the entire day
            from datetime import timedelta
            date_to_obj = date_to_obj + timedelta(days=1)
            filtered_requests = filtered_requests.filter(created_at__lt=date_to_obj)
            filtered_transactions = filtered_transactions.filter(created_at__lt=date_to_obj)
        except ValueError:
            pass
    
    # Apply search filter
    if search_query:
        filtered_requests = filtered_requests.filter(
            Q(request_id__icontains=search_query) |
            Q(supply__name__icontains=search_query) |
            Q(user__username__icontains=search_query)
        )
        filtered_supplies = filtered_supplies.filter(
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(category__name__icontains=search_query)
        )
    
    # Apply status filter for requests
    if status_filter:
        # Support regular SupplyRequest.status values
        if status_filter in dict(SupplyRequest.STATUS_CHOICES).keys():
            filtered_requests = filtered_requests.filter(status=status_filter)
        # Support borrowed/returned pseudo-statuses derived from BorrowedItem
        elif status_filter in ['borrowed', 'returned']:
            borrowed_qs = BorrowedItem.objects.filter(
                supply=OuterRef('supply'),
                borrower=OuterRef('user')
            )
            if status_filter == 'borrowed':
                borrowed_qs = borrowed_qs.filter(returned_at__isnull=True)
            else:
                borrowed_qs = borrowed_qs.filter(returned_at__isnull=False)

            filtered_requests = filtered_requests.filter(Exists(borrowed_qs), purpose__startswith='[BORROWING]')
        else:
            filtered_requests = filtered_requests.none()
    
    # Order by creation date
    filtered_requests = filtered_requests.order_by('-created_at')
    filtered_transactions = filtered_transactions.select_related('supply', 'performed_by').order_by('-created_at')
    
    # Get unique status choices for the filter and include borrowed/returned
    request_statuses = list(SupplyRequest.STATUS_CHOICES) + [
        ('borrowed', 'Borrowed Item'),
        ('returned', 'Returned Item')
    ]
    
    context = {
        'total_supplies': total_supplies,
        'low_stock_items': low_stock_items,
        'total_requests': total_requests,
        'pending_requests': pending_requests,
        'released_requests': released_requests,
        'recent_requests': recent_requests,
        'recent_transactions': recent_transactions,
        'report_type': report_type,
        'filtered_requests': filtered_requests,
        'filtered_transactions': filtered_transactions,
        'filtered_supplies': filtered_supplies,
        'date_from': date_from,
        'date_to': date_to,
        'search_query': search_query,
        'status_filter': status_filter,
        'request_statuses': request_statuses,
    }
    
    # If this is an HTMX request for filtered data, return partial
    if request.htmx:
        if report_type == 'requests':
            return render(request, 'inventory/partials/reports_requests_table.html', context)
        elif report_type == 'transactions':
            return render(request, 'inventory/partials/reports_transactions_table.html', context)
        elif report_type == 'supplies':
            return render(request, 'inventory/partials/reports_supplies_table.html', context)
    
    return render(request, 'inventory/reports.html', context)

@login_required
def export_supplies_csv(request):
    """Export supplies to CSV format with filters"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to export reports.')
        return redirect('dashboard')
    
    # Get filter parameters
    search_query = request.GET.get('search', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    
    # Start with all supplies
    supplies = Supply.objects.select_related('category').all()
    
    # Apply search filter
    if search_query:
        supplies = supplies.filter(
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(category__name__icontains=search_query)
        )
    
    # Apply date filters
    if date_from:
        try:
            from datetime import datetime
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            supplies = supplies.filter(created_at__gte=date_from_obj)
        except ValueError:
            pass
    
    if date_to:
        try:
            from datetime import datetime
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
            from datetime import timedelta
            date_to_obj = date_to_obj + timedelta(days=1)
            supplies = supplies.filter(created_at__lt=date_to_obj)
        except ValueError:
            pass
    
    # Create the HttpResponse object with CSV header
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="supplies_report.csv"'
    
    # Create a CSV writer
    writer = csv.writer(response)
    
    # Write the header row
    writer.writerow([
        'ID', 'Name', 'Category', 'Description', 'Quantity', 
        'Min Stock Level', 'Unit', 'Cost Per Unit', 'Location', 
        'Created At', 'Updated At'
    ])
    
    # Write data rows
    for supply in supplies:
        writer.writerow([
            supply.id,
            supply.name,
            supply.category.name if supply.category else '',
            supply.description,
            supply.quantity,
            supply.min_stock_level,
            supply.unit,
            supply.cost_per_unit,
            supply.location,
            supply.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            supply.updated_at.strftime('%Y-%m-%d %H:%M:%S')
        ])
    
    return response

@login_required
def export_requests_csv(request):
    """Export supply requests to CSV format with filters"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to export reports.')
        return redirect('dashboard')
    
    # Get filter parameters
    search_query = request.GET.get('search', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    status_filter = request.GET.get('status', '')
    
    # Start with all requests
    requests = SupplyRequest.objects.select_related('user', 'supply', 'approved_by', 'released_by').all()
    
    # Apply search filter
    if search_query:
        requests = requests.filter(
            Q(request_id__icontains=search_query) |
            Q(supply__name__icontains=search_query) |
            Q(user__username__icontains=search_query)
        )
    
    # Apply date filters
    if date_from:
        try:
            from datetime import datetime
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            requests = requests.filter(created_at__gte=date_from_obj)
        except ValueError:
            pass
    
    if date_to:
        try:
            from datetime import datetime
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
            from datetime import timedelta
            date_to_obj = date_to_obj + timedelta(days=1)
            requests = requests.filter(created_at__lt=date_to_obj)
        except ValueError:
            pass
    
    # Apply status filter
    if status_filter:
        # Support regular SupplyRequest.status values
        if status_filter in dict(SupplyRequest.STATUS_CHOICES).keys():
            requests = requests.filter(status=status_filter)
        # Support borrowed/returned pseudo-statuses which are derived from BorrowedItem
        elif status_filter in ['borrowed', 'returned']:
            # Build an Exists subquery to find BorrowedItem matching the request's supply and user
            borrowed_qs = BorrowedItem.objects.filter(
                supply=OuterRef('supply'),
                borrower=OuterRef('user')
            )
            if status_filter == 'borrowed':
                borrowed_qs = borrowed_qs.filter(returned_at__isnull=True)
            else:
                borrowed_qs = borrowed_qs.filter(returned_at__isnull=False)

            requests = requests.filter(Exists(borrowed_qs), purpose__startswith='[BORROWING]')
        else:
            # Unrecognized status - no results
            requests = requests.none()
    
    # Create the HttpResponse object with CSV header
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="requests_report.csv"'
    
    # Create a CSV writer
    writer = csv.writer(response)
    
    # Write the header row
    writer.writerow([
        'Request ID', 'User', 'Supply', 'Quantity Requested', 
        'Purpose', 'Status', 'Approved By', 'Approved At', 
        'Released By', 'Released At', 'Created At', 'Updated At'
    ])
    
    # Write data rows
    for req in requests:
        writer.writerow([
            req.request_id,
            req.user.username,
            req.supply.name,
            req.quantity_requested,
            req.purpose,
            req.status,
            req.approved_by.username if req.approved_by else '',
            req.approved_at.strftime('%Y-%m-%d %H:%M:%S') if req.approved_at else '',
            req.released_by.username if req.released_by else '',
            req.released_at.strftime('%Y-%m-%d %H:%M:%S') if req.released_at else '',
            req.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            req.updated_at.strftime('%Y-%m-%d %H:%M:%S')
        ])
    
    return response

@login_required
def export_transactions_csv(request):
    """Export inventory transactions to CSV format with filters"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to export reports.')
        return redirect('dashboard')
    
    # Get filter parameters
    search_query = request.GET.get('search', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    
    # Start with all transactions
    transactions = InventoryTransaction.objects.select_related('supply', 'performed_by').all()
    
    # Apply search filter
    if search_query:
        transactions = transactions.filter(
            Q(supply__name__icontains=search_query) |
            Q(reason__icontains=search_query) |
            Q(performed_by__username__icontains=search_query)
        )
    
    # Apply date filters
    if date_from:
        try:
            from datetime import datetime
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            transactions = transactions.filter(created_at__gte=date_from_obj)
        except ValueError:
            pass
    
    if date_to:
        try:
            from datetime import datetime
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
            from datetime import timedelta
            date_to_obj = date_to_obj + timedelta(days=1)
            transactions = transactions.filter(created_at__lt=date_to_obj)
        except ValueError:
            pass
    
    # Create the HttpResponse object with CSV header
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="transactions_report.csv"'
    
    # Create a CSV writer
    writer = csv.writer(response)
    
    # Write the header row
    writer.writerow([
        'ID', 'Supply', 'Transaction Type', 'Quantity', 
        'Previous Quantity', 'New Quantity', 'Reason', 
        'Performed By', 'Created At'
    ])
    
    # Write data rows
    for transaction in transactions:
        writer.writerow([
            transaction.id,
            transaction.supply.name,
            transaction.transaction_type,
            transaction.quantity,
            transaction.previous_quantity,
            transaction.new_quantity,
            transaction.reason,
            transaction.performed_by.username,
            transaction.created_at.strftime('%Y-%m-%d %H:%M:%S')
        ])
    
    return response

@login_required
@require_http_methods(['POST'])
def generate_qr_code(request, pk):
    """Generate QR code for a single supply item"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to generate QR codes.')
        return redirect('supply_list')
    
    supply = get_object_or_404(Supply, pk=pk)
    
    try:
        supply.generate_qr_code()
        messages.success(request, f'QR code generated successfully for {supply.name}.')
    except Exception as e:
        messages.error(request, f'Failed to generate QR code: {str(e)}')
    
    # Redirect back to the referring page or supply detail
    next_url = request.GET.get('next') or request.META.get('HTTP_REFERER')
    if next_url:
        return redirect(next_url)
    return redirect('supply_detail', pk=supply.pk)

@login_required
@require_http_methods(['POST'])
def generate_qr_codes_bulk(request):
    """Generate QR codes for multiple supply items"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to generate QR codes.')
        return redirect('supply_list')
    
    supply_ids = request.POST.getlist('supply_ids')
    
    if not supply_ids:
        messages.error(request, 'No supplies selected for QR code generation.')
        return redirect('supply_list')
    
    generated_count = 0
    failed_count = 0
    
    for supply_id in supply_ids:
        try:
            supply = Supply.objects.get(pk=supply_id)
            if not supply.qr_code:
                supply.generate_qr_code()
                generated_count += 1
        except Supply.DoesNotExist:
            failed_count += 1
        except Exception as e:
            failed_count += 1
    
    if generated_count > 0:
        messages.success(request, f'Successfully generated QR codes for {generated_count} supply item(s).')
    if failed_count > 0:
        messages.error(request, f'Failed to generate QR codes for {failed_count} supply item(s).')
    
    # Redirect back to the referring page or supply list
    next_url = request.GET.get('next') or request.META.get('HTTP_REFERER')
    if next_url:
        return redirect(next_url)
    return redirect('supply_list')

@login_required
def supply_delete(request, pk):
    if request.user.role not in ['admin']:
        messages.error(request, 'You do not have permission to delete supplies.')
        return redirect('supply_list')
    
    supply = get_object_or_404(Supply, pk=pk)
    
    if request.method == 'POST':
        supply_name = supply.name
        supply.delete()
        messages.success(request, f'Supply "{supply_name}" deleted successfully.')
        return redirect('supply_list')
    
    return redirect('supply_detail', pk=pk)

@login_required
@require_POST
def bulk_delete_supplies(request):
    """Delete multiple supplies at once"""
    if request.user.role not in ['admin', 'gso_staff']:
        return JsonResponse({'success': False, 'error': 'You do not have permission to delete supplies.'}, status=403)
    
    try:
        data = json.loads(request.body)
        supply_ids = data.get('supply_ids', [])
        
        if not supply_ids:
            return JsonResponse({'success': False, 'error': 'No supplies selected'})
        
        # Validate that all IDs exist and get the supplies
        supplies = Supply.objects.filter(id__in=supply_ids)
        
        if len(supplies) != len(supply_ids):
            return JsonResponse({'success': False, 'error': 'Some supplies could not be found'})
        
        # Delete the supplies
        deleted_count = len(supplies)
        supplies.delete()
        
        return JsonResponse({
            'success': True,
            'message': f'Successfully deleted {deleted_count} supply item{"s" if deleted_count > 1 else ""}'
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'Error deleting supplies: {str(e)}'})

@login_required
def get_qr_code(request, pk):
    """Get QR code for a supply item (AJAX endpoint)"""
    supply = get_object_or_404(Supply, pk=pk)
    
    # Generate QR code if it doesn't exist
    if not supply.qr_code:
        try:
            supply.generate_qr_code()
        except Exception as e:
            return JsonResponse({'success': False, 'error': f'Failed to generate QR code: {str(e)}'})
    
    # Make sure we return the QR code URL even if it already existed
    if supply.qr_code:
        return JsonResponse({
            'success': True,
            'qr_url': supply.qr_code.url,
            'supply_name': supply.name,
            'supply_id': supply.id
        })
    else:
        return JsonResponse({'success': False, 'error': 'QR code not available'})

def landing_page(request):
    """Landing page for non-authenticated users"""
    if request.user.is_authenticated:
        return redirect('dashboard')
    
    # Get real statistics from the database
    total_supplies = Supply.objects.count()
    total_requests = SupplyRequest.objects.count()
    active_users = User.objects.count()
    
    # Calculate satisfaction rate (simulated)
    satisfaction_rate = 98 if total_requests > 0 else 0
    
    context = {
        'total_supplies': total_supplies,
        'total_requests': total_requests,
        'active_users': active_users,
        'satisfaction_rate': satisfaction_rate,
    }
    
    return render(request, 'landing.html', context)

@login_required
def export_supplies_pdf(request):
    """Export supplies to PDF format with filters"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to export reports.')
        return redirect('dashboard')
    
    # Get filter parameters
    search_query = request.GET.get('search', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    
    # Start with all supplies
    supplies = Supply.objects.select_related('category').all()
    
    # Apply search filter
    if search_query:
        supplies = supplies.filter(
            Q(name__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(category__name__icontains=search_query)
        )
    
    # Apply date filters
    if date_from:
        try:
            from datetime import datetime
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            supplies = supplies.filter(created_at__gte=date_from_obj)
        except ValueError:
            pass
    
    if date_to:
        try:
            from datetime import datetime
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
            from datetime import timedelta
            date_to_obj = date_to_obj + timedelta(days=1)
            supplies = supplies.filter(created_at__lt=date_to_obj)
        except ValueError:
            pass
    
    # Create the HttpResponse object with PDF header
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="supplies_report.pdf"'
    
    # Create a PDF document
    doc = SimpleDocTemplate(response, pagesize=A4)
    elements = []
    
    # Sample stylesheet
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=18,
        spaceAfter=30,
        alignment=1  # Center alignment
    )
    
    # Title
    title = Paragraph("Supplies Report", title_style)
    elements.append(title)
    elements.append(Spacer(1, 0.25*inch))
    
    # Add filter info
    if search_query or date_from or date_to:
        filter_text = "Filters Applied: "
        filters = []
        if search_query:
            filters.append(f"Search: {search_query}")
        if date_from:
            filters.append(f"From: {date_from}")
        if date_to:
            filters.append(f"To: {date_to}")
        filter_text += " | ".join(filters)
        elements.append(Paragraph(filter_text, styles['Normal']))
        elements.append(Spacer(1, 0.2*inch))
    
    # Table data
    data = [['ID', 'Name', 'Category', 'Quantity', 'Min Stock', 'Unit', 'Location']]
    for supply in supplies:
        data.append([
            str(supply.id),
            supply.name,
            supply.category.name if supply.category else '',
            str(supply.quantity),
            str(supply.min_stock_level),
            supply.unit,
            supply.location
        ])
    
    # Create table
    table = Table(data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    
    elements.append(table)
    
    # Build PDF
    doc.build(elements)
    
    return response

@login_required
def export_requests_pdf(request):
    """Export supply requests to PDF format with filters"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to export reports.')
        return redirect('dashboard')
    
    # Get filter parameters
    search_query = request.GET.get('search', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    status_filter = request.GET.get('status', '')
    
    # Start with all requests
    requests = SupplyRequest.objects.select_related('user', 'supply').all()
    
    # Apply search filter
    if search_query:
        requests = requests.filter(
            Q(request_id__icontains=search_query) |
            Q(supply__name__icontains=search_query) |
            Q(user__username__icontains=search_query)
        )
    
    # Apply date filters
    if date_from:
        try:
            from datetime import datetime
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            requests = requests.filter(created_at__gte=date_from_obj)
        except ValueError:
            pass
    
    if date_to:
        try:
            from datetime import datetime
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
            from datetime import timedelta
            date_to_obj = date_to_obj + timedelta(days=1)
            requests = requests.filter(created_at__lt=date_to_obj)
        except ValueError:
            pass
    
    # Apply status filter
    if status_filter:
        requests = requests.filter(status=status_filter)
    
    # Create the HttpResponse object with PDF header
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="requests_report.pdf"'
    
    # Create a PDF document
    doc = SimpleDocTemplate(response, pagesize=A4)
    elements = []
    
    # Sample stylesheet
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=18,
        spaceAfter=30,
        alignment=1  # Center alignment
    )
    
    # Title
    title = Paragraph("Supply Requests Report", title_style)
    elements.append(title)
    elements.append(Spacer(1, 0.25*inch))
    
    # Add filter info
    if search_query or date_from or date_to or status_filter:
        filter_text = "Filters Applied: "
        filters = []
        if search_query:
            filters.append(f"Search: {search_query}")
        if date_from:
            filters.append(f"From: {date_from}")
        if date_to:
            filters.append(f"To: {date_to}")
        if status_filter:
            filters.append(f"Status: {status_filter}")
        filter_text += " | ".join(filters)
        elements.append(Paragraph(filter_text, styles['Normal']))
        elements.append(Spacer(1, 0.2*inch))
    
    # Table data
    data = [['Request ID', 'User', 'Supply', 'Quantity', 'Status', 'Created']]
    for req in requests:
        data.append([
            req.request_id,
            req.user.username,
            req.supply.name,
            str(req.quantity_requested),
            req.status.title(),
            req.created_at.strftime('%Y-%m-%d')
        ])
    
    # Create table
    table = Table(data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    
    elements.append(table)
    
    # Build PDF
    doc.build(elements)
    
    return response

@login_required
def export_transactions_pdf(request):
    """Export inventory transactions to PDF format with filters"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to export reports.')
        return redirect('dashboard')
    
    # Get filter parameters
    search_query = request.GET.get('search', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    
    # Start with all transactions
    transactions = InventoryTransaction.objects.select_related('supply', 'performed_by').all()
    
    # Apply search filter
    if search_query:
        transactions = transactions.filter(
            Q(supply__name__icontains=search_query) |
            Q(reason__icontains=search_query) |
            Q(performed_by__username__icontains=search_query)
        )
    
    # Apply date filters
    if date_from:
        try:
            from datetime import datetime
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            transactions = transactions.filter(created_at__gte=date_from_obj)
        except ValueError:
            pass
    
    if date_to:
        try:
            from datetime import datetime
            date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
            from datetime import timedelta
            date_to_obj = date_to_obj + timedelta(days=1)
            transactions = transactions.filter(created_at__lt=date_to_obj)
        except ValueError:
            pass
    
    # Create the HttpResponse object with PDF header
    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="transactions_report.pdf"'
    
    # Create a PDF document
    doc = SimpleDocTemplate(response, pagesize=A4)
    elements = []
    
    # Sample stylesheet
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=18,
        spaceAfter=30,
        alignment=1  # Center alignment
    )
    
    # Title
    title = Paragraph("Inventory Transactions Report", title_style)
    elements.append(title)
    elements.append(Spacer(1, 0.25*inch))
    
    # Add filter info
    if search_query or date_from or date_to:
        filter_text = "Filters Applied: "
        filters = []
        if search_query:
            filters.append(f"Search: {search_query}")
        if date_from:
            filters.append(f"From: {date_from}")
        if date_to:
            filters.append(f"To: {date_to}")
        filter_text += " | ".join(filters)
        elements.append(Paragraph(filter_text, styles['Normal']))
        elements.append(Spacer(1, 0.2*inch))
    
    # Table data
    data = [['Supply', 'Type', 'Quantity', 'Previous', 'New', 'Performed By', 'Date']]
    for transaction in transactions:
        data.append([
            transaction.supply.name,
            transaction.transaction_type.title(),
            str(transaction.quantity),
            str(transaction.previous_quantity),
            str(transaction.new_quantity),
            transaction.performed_by.username,
            transaction.created_at.strftime('%Y-%m-%d')
        ])
    
    # Create table
    table = Table(data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('FONTSIZE', (0, 1), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    
    elements.append(table)
    
    # Build PDF
    doc.build(elements)
    
    return response

@login_required
def user_management(request):
    if request.user.role != 'admin':
        messages.error(request, 'You do not have permission to access this page.')
        return redirect('dashboard')
    
    # Get all users with pending approval
    pending_users = User.objects.filter(approval_status='pending')
    
    # Get all users (for management)
    all_users = User.objects.all()
    
    # Filter users based on search
    search = request.GET.get('search', '')
    if search:
        pending_users = pending_users.filter(
            Q(username__icontains=search) | 
            Q(email__icontains=search) |
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search)
        )
        all_users = all_users.filter(
            Q(username__icontains=search) | 
            Q(email__icontains=search) |
            Q(first_name__icontains=search) |
            Q(last_name__icontains=search)
        )
    
    context = {
        'pending_users': pending_users,
        'all_users': all_users,
        'search': search,
    }
    
    return render(request, 'inventory/user_management.html', context)

@login_required
def approve_user(request, user_id):
    if request.user.role != 'admin':
        messages.error(request, 'You do not have permission to perform this action.')
        return redirect('dashboard')
    
    user = get_object_or_404(User, id=user_id)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'approve':
            user.approval_status = 'approved'
            user.save()
            messages.success(request, f'User {user.username} has been approved.')
        elif action == 'reject':
            user.approval_status = 'rejected'
            user.save()
            messages.success(request, f'User {user.username} has been rejected.')
    
    return redirect('user_management')

@login_required
def toggle_user_active(request, user_id):
    if request.user.role != 'admin':
        messages.error(request, 'You do not have permission to perform this action.')
        return redirect('dashboard')
    
    user = get_object_or_404(User, id=user_id)
    
    if request.method == 'POST':
        user.is_active = not user.is_active
        user.save()
        status = "activated" if user.is_active else "deactivated"
        messages.success(request, f'User {user.username} has been {status}.')
    
    return redirect('user_management')

@login_required
def borrowed_items_list(request):
    """
    Display a list of borrowed items with their duration information
    """
    user = request.user
    
    # For admin and GSO staff, show all borrowed items
    # For department users, show only their borrowed items
    if user.role in ['admin', 'gso_staff']:
        borrowed_items = BorrowedItem.objects.all()
    else:
        borrowed_items = BorrowedItem.objects.filter(borrower=user)
    
    # Filter by return status
    status_filter = request.GET.get('status', '')  # Default to showing all items
    if status_filter == 'returned':
        borrowed_items = borrowed_items.filter(returned_at__isnull=False)
    elif status_filter == 'borrowed':
        borrowed_items = borrowed_items.filter(returned_at__isnull=True)
    
    # Search functionality
    search = request.GET.get('search', '')
    if search:
        borrowed_items = borrowed_items.filter(
            Q(supply__name__icontains=search) |
            Q(borrower__username__icontains=search) |
            Q(borrower__first_name__icontains=search) |
            Q(borrower__last_name__icontains=search)
        )
    
    # Enhance borrowed items with borrowing request information for QR code access
    enhanced_borrowed_items = []
    for item in borrowed_items:
        # Find the corresponding borrowing request
        borrowing_request = None
        if item.supply and item.borrower:
            # Look for a borrowing request with the same supply and borrower
            # that was approved around the time the item was borrowed
            time_window_start = item.borrowed_at - timezone.timedelta(hours=24)
            time_window_end = item.borrowed_at + timezone.timedelta(hours=24)
            
            borrowing_request = SupplyRequest.objects.filter(
                supply=item.supply,
                user=item.borrower,
                purpose__startswith='[BORROWING]',
                status__in=['approved', 'released'],
                approved_at__gte=time_window_start,
                approved_at__lte=time_window_end
            ).first()
        
        # Add the borrowing request to the item for template use
        item.borrowing_request = borrowing_request
        enhanced_borrowed_items.append(item)
    
    context = {
        'borrowed_items': enhanced_borrowed_items,
        'status_filter': status_filter,
        'search': search,
    }
    
    if request.htmx:
        return render(request, 'inventory/partials/borrowed_items_list.html', context)
    
    return render(request, 'inventory/borrowed_items_list.html', context)

@login_required
def manage_borrowed_item(request, pk):
    """
    Allow GSO/admin users to manage borrowed items (mark as returned, add notes, etc.)
    """
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to manage borrowed items.')
        return redirect('borrowed_items_list')
    
    borrowed_item = get_object_or_404(BorrowedItem, pk=pk)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'mark_returned':
            # Mark the item as returned
            if not borrowed_item.is_returned:
                borrowed_item.returned_at = timezone.now()
                borrowed_item.location_when_returned = request.POST.get('location', borrowed_item.location_when_borrowed)
                borrowed_item.notes = request.POST.get('notes', borrowed_item.notes)
                borrowed_item.save()
                
                # Update the supply quantity
                supply = borrowed_item.supply
                previous_quantity = supply.quantity
                supply.quantity += borrowed_item.borrowed_quantity
                supply.save()
                
                # Log inventory transaction
                InventoryTransaction.objects.create(
                    supply=supply,
                    transaction_type='in',
                    quantity=borrowed_item.borrowed_quantity,
                    previous_quantity=previous_quantity,
                    new_quantity=supply.quantity,
                    reason=f"Returned borrowed item (ID: {borrowed_item.id})",
                    performed_by=request.user
                )
                
                messages.success(request, f'{borrowed_item.supply.name} marked as returned successfully.')
            else:
                messages.info(request, f'{borrowed_item.supply.name} is already marked as returned.')
        
        elif action == 'add_note':
            # Add a note to the borrowed item
            note = request.POST.get('note', '').strip()
            if note:
                if borrowed_item.notes:
                    borrowed_item.notes += f"\n\n[{request.user.username} - {timezone.now().strftime('%Y-%m-%d %H:%M')}]: {note}"
                else:
                    borrowed_item.notes = f"[{request.user.username} - {timezone.now().strftime('%Y-%m-%d %H:%M')}]: {note}"
                borrowed_item.save()
                messages.success(request, 'Note added successfully.')
            else:
                messages.error(request, 'Please enter a note.')
        
        return redirect('manage_borrowed_item', pk=pk)
    
    # Get transaction history for this supply
    transactions = borrowed_item.supply.transactions.order_by('-created_at')[:10]
    
    context = {
        'borrowed_item': borrowed_item,
        'transactions': transactions,
    }
    
    return render(request, 'inventory/manage_borrowed_item.html', context)

@login_required
def department_request_history(request):
    """
    Display request history for department users
    """
    if request.user.role != 'department_user':
        messages.error(request, 'You do not have permission to access this page.')
        return redirect('dashboard')
    
    # Get all requests for the current user
    requests = SupplyRequest.objects.filter(user=request.user)
    
    # Filter by status
    status_filter = request.GET.get('status', '')
    if status_filter:
        requests = requests.filter(status=status_filter)
    
    # Search functionality
    search = request.GET.get('search', '')
    if search:
        requests = requests.filter(
            Q(request_id__icontains=search) |
            Q(supply__name__icontains=search) |
            Q(purpose__icontains=search)
        )
    
    # Order by creation date (newest first)
    requests = requests.order_by('-created_at')
    
    # Group batch requests by creation time (within 5 seconds = batch submission)
    requests_list = list(requests)
    grouped_requests = []
    
    i = 0
    while i < len(requests_list):
        current = requests_list[i]
        batch = [current]
        
        # Look ahead to see if next requests are part of the same batch
        # (created within 5 seconds and same purpose)
        j = i + 1
        while j < len(requests_list):
            next_req = requests_list[j]
            time_diff = (current.created_at - next_req.created_at).total_seconds()
            
            # If requests are within 5 seconds and have same purpose, they're in same batch
            if abs(time_diff) <= 5 and current.purpose == next_req.purpose:
                batch.append(next_req)
                j += 1
            else:
                break
        
        grouped_requests.append({
            'is_batch': len(batch) > 1,
            'batch_id': current.created_at.strftime('%Y%m%d%H%M%S'),
            'items': batch
        })
        
        i = j if j > i + 1 else i + 1
    
    context = {
        'grouped_requests': grouped_requests,
        'status_filter': status_filter,
        'search': search,
    }
    
    if request.htmx:
        return render(request, 'inventory/partials/department_request_list.html', context)
    
    return render(request, 'inventory/department_request_history.html', context)

@login_required
def approve_borrow_request(request, pk):
    """
    GSO staff approval for borrowing requests with date selection.
    After approval, creates the BorrowedItem record.
    """
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to approve borrow requests.')
        return redirect('request_list')
    
    supply_request = get_object_or_404(SupplyRequest, pk=pk)
    
    # Ensure this is a borrowing request
    if not supply_request.purpose.startswith('[BORROWING]'):
        messages.error(request, 'This is not a borrowing request.')
        return redirect('request_detail', pk=pk)
    
    # Ensure request is still pending
    if supply_request.status != 'pending':
        messages.error(request, 'This request has already been processed.')
        return redirect('request_detail', pk=pk)
    
    if request.method == 'POST':
        # Determine which action the user took: 'approve' (approve only) or 'create' (approve + create borrow record)
        action = request.POST.get('action', 'create')
        if action == 'approve':
            # Approve the request but do not create the borrow record / release the item
            supply_request.status = 'approved'
            supply_request.approved_by = request.user
            supply_request.approved_at = timezone.now()
            supply_request.save()
            messages.success(request, f'Request {supply_request.request_id} approved. Create borrow record on the approve page when ready.')
            return redirect('request_detail', pk=pk)
        else:
            # Default behavior: create BorrowedItem and mark as released (existing behavior)
            form = BorrowedItemForm(request.POST)
            if form.is_valid():
                borrowed_item = form.save(commit=False)
                borrowed_item.supply = supply_request.supply
                borrowed_item.borrower = supply_request.user
                borrowed_item.borrowed_quantity = supply_request.quantity_requested
                borrowed_item.save()

                # Update supply request status to released
                supply_request.status = 'released'
                supply_request.approved_by = request.user
                supply_request.approved_at = timezone.now()
                supply_request.released_by = request.user
                supply_request.released_at = timezone.now()
                supply_request.save()

                # Update supply quantity
                previous_quantity = supply_request.supply.quantity
                supply_request.supply.quantity -= supply_request.quantity_requested
                supply_request.supply.save()

                # Log inventory transaction
                InventoryTransaction.objects.create(
                    supply=supply_request.supply,
                    transaction_type='out',
                    quantity=-supply_request.quantity_requested,
                    previous_quantity=previous_quantity,
                    new_quantity=supply_request.supply.quantity,
                    reason=f"Borrowed item (ID: {borrowed_item.id}) - Return by {borrowed_item.return_deadline}",
                    performed_by=request.user
                )

                messages.success(
                    request,
                    f'Borrow request approved and borrow record created. {supply_request.user.username} must return {borrowed_item.supply.name} by {borrowed_item.return_deadline}.'
                )
                return redirect('request_detail', pk=pk)
            else:
                messages.error(request, 'Please correct the errors below.')
    else:
        form = BorrowedItemForm()
    
    # Extract borrow duration from request purpose
    borrow_duration = 3  # default
    if 'Borrow Duration:' in supply_request.purpose:
        try:
            duration_str = supply_request.purpose.split('Borrow Duration: ')[-1].split(' ')[0]
            borrow_duration = int(duration_str)
        except:
            pass
    
    context = {
        'form': form,
        'supply_request': supply_request,
        'borrow_duration': borrow_duration,
    }
    
    return render(request, 'inventory/approve_borrow_request.html', context)

@login_required
def request_borrow_item(request):
    """
    Allow department users to request to borrow items (requires GSO approval)
    """
    if request.user.role != 'department_user':
        messages.error(request, 'Only department users can request to borrow items.')
        return redirect('dashboard')
    
    # Check if user has overdue items
    # Check if user has overdue items; do not redirect — show form but block submission
    has_overdues = has_overdue_items(request.user)
    overdue_items = get_user_overdue_items(request.user) if has_overdues else None
    if has_overdues:
        messages.error(
            request,
            f'You have {overdue_items.count()} overdue item(s). Please return them before borrowing new items.'
        )

    if request.method == 'POST':
        # Prevent submission if user has overdue items
        if has_overdues:
            messages.error(request, 'Cannot submit borrow request while you have overdue items. Return them first.')
            form = BorrowRequestForm(request.POST)
        else:
            form = BorrowRequestForm(request.POST)
            if form.is_valid():
                supply_request = form.save(commit=False)
                supply_request.user = request.user
                supply_request.status = 'pending'

                # Extract the borrow duration from the form
                borrow_duration = form.cleaned_data['borrow_duration_days']

                # Mark this as a borrowing request
                supply_request.purpose = f"[BORROWING] {supply_request.purpose}\n\nBorrow Duration: {borrow_duration} days"
                supply_request.save()

                # Generate borrowing QR code
                supply_request.generate_borrowing_qr_code()

                messages.success(
                    request,
                    f'Borrow request submitted successfully. GSO staff will review and approve your request.'
                )
                return redirect('request_detail', pk=supply_request.pk)
            else:
                messages.error(request, 'Please correct the errors below.')
    else:
        form = BorrowRequestForm()
    
    # Prepare supplies data for the template - only non-consumable items with stock
    # Separate into equipment and materials
    equipment_supplies = []
    material_supplies = []
    
    for s in Supply.objects.filter(quantity__gt=0, is_consumable=False).order_by('name'):
        supply_data = {
            'id': s.pk,
            'name': s.name,
            'stock': s.quantity,
            'unit': s.unit or 'pieces',
            'category': s.category.name,
            'location': s.location,
            'is_consumable': False
        }
        
        if s.category.is_material:
            material_supplies.append(supply_data)
        else:
            equipment_supplies.append(supply_data)
    
    context = {
        'form': form,
        'equipment_supplies_json': json.dumps(equipment_supplies),
        'material_supplies_json': json.dumps(material_supplies),
        'non_consumable_supplies': json.dumps(equipment_supplies + material_supplies),
        'can_borrow': not has_overdues,
        'overdue_items': overdue_items,
    }
    
    return render(request, 'inventory/request_borrow_item.html', context)

@login_required
def request_borrow_batch(request):
    """
    Allow department users to request to borrow multiple items at once
    """
    if request.user.role != 'department_user':
        messages.error(request, 'Only department users can request to borrow items.')
        return redirect('dashboard')
    
    # Check if user has overdue items
    has_overdues = has_overdue_items(request.user)
    overdue_items = get_user_overdue_items(request.user) if has_overdues else None
    if has_overdues:
        messages.error(
            request,
            f'You have {overdue_items.count()} overdue item(s). Please return them before borrowing new items.'
        )

    if request.method == 'POST':
        # Prevent submission if user has overdue items
        if has_overdues:
            messages.error(request, 'Cannot submit borrow request while you have overdue items. Return them first.')
        else:
            # Handle bulk borrowing request similar to bulk supply request
            supply_ids = request.POST.getlist('supply_ids')
            borrow_duration = request.POST.get('borrow_duration', '3')
            purpose = request.POST.get('purpose', '')
            
            if not purpose:
                messages.error(request, "Purpose is required for borrow requests.")
            elif not supply_ids:
                messages.error(request, "Please select at least one item to borrow.")
            else:
                try:
                    for s_id in supply_ids:
                        qty_str = request.POST.get(f'quantity_{s_id}', '0')
                        qty = int(qty_str) if qty_str else 0
                        if qty > 0:
                            supply = get_object_or_404(Supply, id=s_id)
                            
                            # Create borrowing request
                            supply_request = SupplyRequest.objects.create(
                                user=request.user,
                                supply=supply,
                                quantity_requested=qty,
                                purpose=f"[BORROWING] {purpose}\n\nBorrow Duration: {borrow_duration} days",
                                status='pending'
                            )
                            
                            # Generate borrowing QR code
                            supply_request.generate_borrowing_qr_code()
                    
                    messages.success(
                        request,
                        f'Borrow request submitted successfully. GSO staff will review and approve your request.'
                    )
                    return redirect('request_list')
                except ValueError as e:
                    messages.error(request, f'Error creating borrow request: {str(e)}')
    
    # Prepare supplies data for the template - only non-consumable items with stock
    equipment_supplies = []
    material_supplies = []
    
    for s in Supply.objects.filter(quantity__gt=0, is_consumable=False).order_by('name'):
        supply_data = {
            'id': s.pk,
            'name': s.name,
            'stock': s.quantity,
            'unit': s.unit or 'pieces',
            'category': s.category.name,
            'location': s.location,
            'is_consumable': False
        }
        
        if s.category.is_material:
            material_supplies.append(supply_data)
        else:
            equipment_supplies.append(supply_data)
    
    context = {
        'equipment_supplies_json': json.dumps(equipment_supplies),
        'material_supplies_json': json.dumps(material_supplies),
        'can_borrow': not has_overdues,
        'overdue_items': overdue_items,
    }
    
    return render(request, 'inventory/request_borrow_batch.html', context)

@login_required
@require_POST
def bulk_delete_borrowed_items(request):
    """
    Delete multiple borrowed items at once (GSO/Admin only)
    """
    if request.user.role not in ['admin', 'gso_staff']:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    
    try:
        item_ids = request.POST.getlist('item_ids')
        
        if not item_ids:
            return JsonResponse({'success': False, 'error': 'No items selected'}, status=400)
        
        # Get the borrowed items to delete
        borrowed_items = BorrowedItem.objects.filter(pk__in=item_ids)
        
        # Process each item
        deleted_count = 0
        for item in borrowed_items:
            # If not returned, restore the supply quantity
            if not item.is_returned:
                supply = item.supply
                previous_quantity = supply.quantity
                supply.quantity += item.borrowed_quantity
                supply.save()
                
                # Log inventory transaction
                InventoryTransaction.objects.create(
                    supply=supply,
                    transaction_type='in',
                    quantity=item.borrowed_quantity,
                    previous_quantity=previous_quantity,
                    new_quantity=supply.quantity,
                    reason=f"Borrowed item (ID: {item.id}) deleted/removed",
                    performed_by=request.user
                )
            
            item.delete()
            deleted_count += 1
        
        if request.htmx:
            messages.success(request, f'{deleted_count} borrowed item(s) deleted successfully.')
            return HttpResponseClientRefresh()
        
        messages.success(request, f'{deleted_count} borrowed item(s) deleted successfully.')
        return redirect('borrowed_items_list')
    
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
def category_list(request):
    """Display list of all supply categories"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to manage categories.')
        return redirect('supply_list')
    
    categories = SupplyCategory.objects.all().order_by('name')
    
    # Count supplies in each category
    categories_with_count = []
    for category in categories:
        supply_count = category.supplies.count()
        categories_with_count.append({
            'category': category,
            'supply_count': supply_count
        })
    
    context = {
        'categories_with_count': categories_with_count,
    }
    return render(request, 'inventory/category_list.html', context)

@login_required
def category_create(request):
    """Create a new supply category"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to create categories.')
        return redirect('category_list')
    
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        is_material = request.POST.get('is_material') == 'on'
        
        if not name:
            messages.error(request, 'Category name is required.')
        elif len(name) < 2:
            messages.error(request, 'Category name must be at least 2 characters long.')
        elif SupplyCategory.objects.filter(name__iexact=name).exists():
            messages.error(request, 'Category with this name already exists.')
        else:
            category = SupplyCategory.objects.create(
                name=name,
                description=description,
                is_material=is_material
            )
            messages.success(request, f'Category "{name}" created successfully.')
            return redirect('category_list')
    
    return render(request, 'inventory/category_form.html', {'action': 'Create'})

@login_required
def category_edit(request, pk):
    """Edit a supply category"""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to edit categories.')
        return redirect('category_list')
    
    category = get_object_or_404(SupplyCategory, pk=pk)
    
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        is_material = request.POST.get('is_material') == 'on'
        
        if not name:
            messages.error(request, 'Category name is required.')
        elif len(name) < 2:
            messages.error(request, 'Category name must be at least 2 characters long.')
        elif SupplyCategory.objects.filter(name__iexact=name).exclude(pk=pk).exists():
            messages.error(request, 'Category with this name already exists.')
        else:
            category.name = name
            category.description = description
            category.is_material = is_material
            category.save()
            messages.success(request, f'Category "{name}" updated successfully.')
            return redirect('category_list')
    
    context = {
        'category': category,
        'action': 'Edit'
    }
    return render(request, 'inventory/category_form.html', context)

@login_required
def category_delete(request, pk):
    """Delete a supply category with optional force deletion of supplies"""
    if request.user.role not in ['admin']:
        messages.error(request, 'You do not have permission to delete categories.')
        return redirect('category_list')
    
    category = get_object_or_404(SupplyCategory, pk=pk)
    
    if request.method == 'POST':
        force_delete = request.POST.get('force_delete') == 'true'
        
        # If category has supplies and force_delete is not checked, don't delete
        if category.supplies.exists() and not force_delete:
            messages.error(request, f'Cannot delete category "{category.name}" because it contains supplies. Check the force delete option to delete everything.')
            return redirect('category_delete', pk=pk)
        
        # If force delete, also delete all supplies in this category
        supplies_to_delete = category.supplies.all().count()
        
        if force_delete and supplies_to_delete > 0:
            # Delete all supplies in the category
            category.supplies.all().delete()
            messages.warning(request, f'Force deleted {supplies_to_delete} suppl{"y" if supplies_to_delete == 1 else "ies"} in "{category.name}".')
        
        category_name = category.name
        category.delete()
        messages.success(request, f'Category "{category_name}" deleted successfully.')
        return redirect('category_list')
    
    context = {
        'category': category,
    }
    return render(request, 'inventory/category_confirm_delete.html', context)

@login_required
@require_POST
def bulk_delete_categories(request):
    """Delete multiple categories at once with optional force deletion"""
    if request.user.role not in ['admin', 'gso_staff']:
        return JsonResponse({'success': False, 'error': 'You do not have permission to delete categories.'}, status=403)
    
    try:
        data = json.loads(request.body)
        category_ids = data.get('category_ids', [])
        force_delete = data.get('force_delete', False)
        
        if not category_ids:
            return JsonResponse({'success': False, 'error': 'No categories selected'})
        
        # Validate that all IDs exist and get the categories
        categories = SupplyCategory.objects.filter(id__in=category_ids)
        
        if len(categories) != len(category_ids):
            return JsonResponse({'success': False, 'error': 'Some categories could not be found'})
        
        deleted_count = 0
        skipped_count = 0
        supplies_deleted = 0
        skipped_categories = []
        
        for category in categories:
            if category.supplies.exists():
                if force_delete:
                    # Force delete the supplies
                    supplies_deleted += category.supplies.count()
                    category.supplies.all().delete()
                    category.delete()
                    deleted_count += 1
                else:
                    # Skip categories with supplies if not force deleting
                    skipped_count += 1
                    skipped_categories.append(category.name)
            else:
                # Delete empty categories
                category.delete()
                deleted_count += 1
        
        message = f'Successfully deleted {deleted_count} category{"" if deleted_count == 1 else "ies"}'
        if supplies_deleted > 0:
            message += f' and {supplies_deleted} suppl{"y" if supplies_deleted == 1 else "ies"}'
        
        error_message = None
        if skipped_categories:
            error_message = f'Skipped {skipped_count} category(ies) with supplies: {", ".join(skipped_categories)}'
        
        return JsonResponse({
            'success': True,
            'message': message,
            'error_message': error_message,
            'deleted_count': deleted_count,
            'supplies_deleted': supplies_deleted,
            'skipped_count': skipped_count
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid request format'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': f'Error deleting categories: {str(e)}'})

@login_required
@require_POST
def create_category_api(request):
    """
    API endpoint to create a new supply category via AJAX
    """
    if request.user.role not in ['admin', 'gso_staff']:
        return JsonResponse({'success': False, 'error': 'Unauthorized'}, status=403)
    
    try:
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        
        if not name:
            return JsonResponse({'success': False, 'error': 'Category name is required'}, status=400)
        
        if len(name) < 2:
            return JsonResponse({'success': False, 'error': 'Category name must be at least 2 characters long'}, status=400)
        
        # Check if category already exists
        if SupplyCategory.objects.filter(name__iexact=name).exists():
            return JsonResponse({'success': False, 'error': 'Category with this name already exists'}, status=400)
        
        # Determine is_material flag (POST from form or JSON)
        is_material = False
        if request.content_type == 'application/json':
            try:
                data = json.loads(request.body)
                is_material = bool(data.get('is_material', False))
            except Exception:
                is_material = False
        else:
            is_material = request.POST.get('is_material') in ['on', 'true', 'True', '1']

        # Create new category
        category = SupplyCategory.objects.create(
            name=name,
            description=description,
            is_material=is_material
        )
        
        return JsonResponse({
            'success': True,
            'category': {
                'id': category.id,
                'name': category.name,
                'description': category.description
            },
            'message': f'Category "{name}" created successfully'
        })
    
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
def get_categories_api(request):
    """
    API endpoint to get all supply categories as JSON
    """
    try:
        categories = SupplyCategory.objects.all().order_by('name').values('id', 'name')
        return JsonResponse({
            'success': True,
            'categories': list(categories)
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
def get_category_supplies_api(request, pk):
    """
    Return JSON list of supplies in a given category.
    """
    try:
        category = get_object_or_404(SupplyCategory, pk=pk)
        supplies_qs = category.supplies.all().values('id', 'name', 'quantity', 'unit')
        supplies = []
        for s in supplies_qs:
            supplies.append({
                'id': s['id'],
                'name': s['name'],
                'quantity': s['quantity'],
                'unit': s['unit'],
                'detail_url': reverse('supply_detail', args=[s['id']])
            })

        return JsonResponse({'success': True, 'category': category.name, 'supplies': supplies})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
@require_http_methods(["POST"])
def get_supply_suggestions(request):
    """
    API endpoint to get AI-powered supply suggestions using Gemini API.
    Returns description, category suggestion, and recommended stock level.
    """
    try:
        data = json.loads(request.body)
        supply_name = data.get('name', '').strip()

        if not supply_name or len(supply_name) < 2:
            return JsonResponse({'error': 'Supply name too short'}, status=400)

        # If Gemini is available and an API key is configured, use it.
        api_key = getattr(settings, 'GEMINI_API_KEY', None)
        if GENAI_AVAILABLE and api_key:
            try:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-3-pro-preview')

                prompt = f"""You are a supply management assistant. Analyze this supply item: "{supply_name}"

Provide ONLY a JSON response (no other text) with these fields:
- description: 1-2 sentence description of what this item is used for
- category: Suggest ONE category (Electronics, Office Supplies, Safety Equipment, Furniture, Cleaning, Educational, Other)
- suggested_quantity: typical stock level (as a number between 10-500)
- unit: Most appropriate unit of measurement (pieces, boxes, packages, sets, etc)

Respond ONLY with valid JSON."""

                response = model.generate_content(prompt)
                response_text = response.text.strip()

                # Clean up the response if it has markdown code blocks
                if response_text.startswith('```'):
                    response_text = response_text.split('```')[1]
                    if response_text.startswith('json'):
                        response_text = response_text[4:]
                response_text = response_text.strip()

                result = json.loads(response_text)

                return JsonResponse({
                    'success': True,
                    'description': result.get('description', ''),
                    'category': result.get('category', ''),
                    'suggested_quantity': result.get('suggested_quantity', 50),
                    'unit': result.get('unit', 'pieces')
                })
            except json.JSONDecodeError as e:
                return JsonResponse({'error': f'Invalid JSON response from API: {str(e)}'}, status=500)
            except Exception as e:
                # If the external API call fails, fall through to a local heuristic fallback
                print(f"[WARNING] Gemini API call failed: {type(e).__name__}: {e}")

        # Fallback heuristics when Gemini or API key is not available
        name_lower = supply_name.lower()
        suggested_quantity = 50
        unit = 'pieces'
        category = 'Other'

        office_keywords = ['pen', 'paper', 'notebook', 'stapler', 'marker', 'scissors', 'envelope']
        electronics_keywords = ['battery', 'charger', 'adapter', 'cable', 'mouse', 'keyboard', 'monitor']
        safety_keywords = ['glove', 'mask', 'helmet', 'goggles']
        cleaning_keywords = ['detergent', 'soap', 'mop', 'broom', 'disinfectant', 'cleaner']
        furniture_keywords = ['chair', 'table', 'desk', 'cabinet']
        educational_keywords = ['projector', 'marker', 'whiteboard', 'chalk']

        if any(k in name_lower for k in office_keywords):
            category = 'Office Supplies'
            suggested_quantity = 100
            unit = 'pieces'
        elif any(k in name_lower for k in electronics_keywords):
            category = 'Electronics'
            suggested_quantity = 20
            unit = 'pieces'
        elif any(k in name_lower for k in safety_keywords):
            category = 'Safety Equipment'
            suggested_quantity = 50
            unit = 'pieces'
        elif any(k in name_lower for k in cleaning_keywords):
            category = 'Cleaning'
            suggested_quantity = 30
            unit = 'bottles'
        elif any(k in name_lower for k in furniture_keywords):
            category = 'Furniture'
            suggested_quantity = 10
            unit = 'sets'
        elif any(k in name_lower for k in educational_keywords):
            category = 'Educational'
            suggested_quantity = 15
            unit = 'pieces'
        else:
            # Default fallback: try to infer plurality
            if supply_name.endswith('s'):
                unit = 'pieces'

        description = f"{supply_name} is a common item used for general {category.lower()} needs." if category != 'Other' else f"{supply_name} is a commonly stocked supply item."

        return JsonResponse({
            'success': True,
            'description': description,
            'category': category,
            'suggested_quantity': suggested_quantity,
            'unit': unit
        })
    except json.JSONDecodeError as e:
        return JsonResponse({'error': f'Invalid JSON in request body: {str(e)}'}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@login_required
def transaction_list(request):
    """View to display inventory transaction history."""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to view transaction history.')
        return redirect('dashboard')
    
    transactions = InventoryTransaction.objects.all().select_related('supply', 'performed_by').order_by('-created_at')
    
    # Simple search
    search = request.GET.get('search', '')
    if search:
        transactions = transactions.filter(
            Q(supply__name__icontains=search) |
            Q(reason__icontains=search) |
            Q(performed_by__username__icontains=search)
        )
    
    # Filtering by type
    txn_type = request.GET.get('type', '')
    if txn_type:
        transactions = transactions.filter(transaction_type=txn_type)

    return render(request, 'inventory/transaction_history.html', {
        'transactions': transactions,
        'search': search,
        'txn_type': txn_type,
    })


@login_required
def supply_restock(request, pk):
    """View to restock a supply item."""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'You do not have permission to restock supplies.')
        return redirect('supply_list')
    
    supply = get_object_or_404(Supply, pk=pk)
    
    if request.method == 'POST':
        try:
            quantity_str = request.POST.get('quantity', '0')
            quantity = int(quantity_str) if quantity_str else 0
            reason = request.POST.get('reason', 'Restock')
            
            if quantity > 0:
                previous_quantity = supply.quantity
                supply.quantity += quantity
                supply.save()
                
                # Log transaction
                InventoryTransaction.objects.create(
                    supply=supply,
                    transaction_type='in',
                    quantity=quantity,
                    previous_quantity=previous_quantity,
                    new_quantity=supply.quantity,
                    reason=reason,
                    performed_by=request.user
                )
                
                messages.success(request, f'Successfully added {quantity} {supply.unit} to {supply.name}.')
                return redirect('supply_detail', pk=pk)
            else:
                messages.error(request, 'Quantity must be greater than zero.')
        except ValueError:
            messages.error(request, 'Invalid quantity provided.')
            
    recent_transactions = supply.transactions.order_by('-created_at')[:10]
    
    return render(request, 'inventory/supply_restock.html', {
        'supply': supply,
        'recent_transactions': recent_transactions,
    })


@login_required
def bulk_request_create(request):
    """View to handle bulk supply/borrowing requests."""
    if request.method == 'POST':
        bulk_action = request.POST.get('bulk_action')
        supply_ids = request.POST.getlist('supply_ids')
        is_borrowing = request.POST.get('is_borrowing') == 'True'
        purpose = request.POST.get('purpose', '')
        
        if bulk_action == 'submit_bulk':
            if not purpose:
                messages.error(request, "Purpose is required for bulk requests.")
                return redirect('supply_list')
                
            for s_id in supply_ids:
                try:
                    qty_str = request.POST.get(f'quantity_{s_id}', '0')
                    qty = int(qty_str) if qty_str else 0
                    if qty > 0:
                        supply = get_object_or_404(Supply, id=s_id)
                        
                        final_purpose = purpose
                        if is_borrowing:
                            if not final_purpose.startswith('[BORROWING]'):
                                final_purpose = f"[BORROWING] {final_purpose}"
                        
                        SupplyRequest.objects.create(
                            user=request.user,
                            supply=supply,
                            quantity_requested=qty,
                            purpose=final_purpose
                        )
                except ValueError:
                    continue
            
            messages.success(request, 'Bulk request submitted successfully.')
            return redirect('request_list')
            
        elif bulk_action == 'start_bulk':
            supplies = Supply.objects.filter(id__in=supply_ids)
            return render(request, 'inventory/bulk_request.html', {
                'supplies': supplies,
                'is_borrowing': is_borrowing,
            })
            
    return redirect('supply_list')


@login_required
@require_POST
def bulk_approve_request(request, group_id):
    """Approve all items in a grouped request."""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'Unauthorized')
        return redirect('request_list')
    
    try:
        user_id, timestamp_str = group_id.split('-', 1)
        target_time = timestamp_str[:12]
        
        requests = SupplyRequest.objects.filter(
            user_id=user_id,
            status='pending'
        )
        
        count = 0
        for req in requests:
            if req.created_at.strftime('%Y%m%d%H%M') == target_time:
                if not req.purpose.startswith('[BORROWING]'):
                    req.status = 'approved'
                    req.approved_by = request.user
                    req.approved_at = timezone.now()
                    req.save()
                    count += 1
        
        messages.success(request, f'Successfully approved {count} items in the group.')
    except Exception as e:
        messages.error(request, f'Error during bulk approval: {str(e)}')
        
    return redirect('request_list')

@login_required
@require_POST
def bulk_reject_request(request, group_id):
    """Reject all items in a grouped request."""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'Unauthorized')
        return redirect('request_list')
    
    reason = request.POST.get('reason', '')
    
    try:
        user_id, timestamp_str = group_id.split('-', 1)
        target_time = timestamp_str[:12]
        
        requests = SupplyRequest.objects.filter(
            user_id=user_id,
            status='pending'
        )
        
        count = 0
        for req in requests:
            if req.created_at.strftime('%Y%m%d%H%M') == target_time:
                req.status = 'rejected'
                req.rejected_reason = reason
                req.approved_by = request.user
                req.approved_at = timezone.now()
                req.save()
                count += 1
        
        messages.success(request, f'Successfully rejected {count} items in the group.')
    except Exception as e:
        messages.error(request, f'Error during bulk rejection: {str(e)}')
        
    return redirect('request_list')

@login_required
@require_POST
def bulk_release_request(request, group_id):
    """Release all approved items in a grouped request."""
    if request.user.role not in ['admin', 'gso_staff']:
        messages.error(request, 'Unauthorized')
        return redirect('request_list')
    
    try:
        user_id, timestamp_str = group_id.split('-', 1)
        target_time = timestamp_str[:12]
        
        requests = SupplyRequest.objects.filter(
            user_id=user_id,
            status='approved'
        )
        
        count = 0
        for req in requests:
            if req.created_at.strftime('%Y%m%d%H%M') == target_time:
                if req.supply.quantity >= req.quantity_requested:
                    previous_quantity = req.supply.quantity
                    req.supply.quantity -= req.quantity_requested
                    req.supply.save()
                    
                    req.status = 'released'
                    req.released_by = request.user
                    req.released_at = timezone.now()
                    req.save()
                    
                    InventoryTransaction.objects.create(
                        supply=req.supply,
                        transaction_type='out',
                        quantity=-req.quantity_requested,
                        previous_quantity=previous_quantity,
                        new_quantity=req.supply.quantity,
                        reason=f"Released for bulk request",
                        performed_by=request.user
                    )
                    count += 1
        
        messages.success(request, f'Successfully released {count} items in the group.')
    except Exception as e:
        messages.error(request, f'Error during bulk release: {str(e)}')
        
    return redirect('request_list')
